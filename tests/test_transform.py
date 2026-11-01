"""The Transform Run's workflow, with stubbed activities (ADR 31, ADR 34)."""

import asyncio
import uuid

from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from football_data_ai_platform.identity.activities import (
    ClubResult,
    IdentityResolutionResult,
    UnlinkedRecord,
)
from football_data_ai_platform.identity.clubs import UnresolvedTeam
from football_data_ai_platform.ingestion.cli import start_transform
from football_data_ai_platform.transform.dbt import DbtBuildResult
from football_data_ai_platform.transform.workflows import (
    THE_REST,
    TRANSFORM_WORKFLOW_ID,
    TransformWorkflow,
)

UNLINKED_TEAM = UnresolvedTeam("understat", "80", "Chelsea", "no Reep team")
UNLINKED_PLAYER = UnlinkedRecord("sofascore", 999, "Nobody", 90, pending=False)
LEFT_OUT = "20 of 40 WARN 2 warn_matches_left_out_of_the_marts .... [WARN 2 in 0.05s]"


class Stubs:
    """Records each activity call; `release` holds the first dbt build until it is set."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.release = asyncio.Event()
        self.release.set()

    @activity.defn(name="build_dbt")
    async def build_dbt(self, selection: list[str]) -> DbtBuildResult:
        self.calls.append(("dbt", *selection))
        await self.release.wait()
        warnings = [LEFT_OUT] if selection == THE_REST else []
        return DbtBuildResult(f"Done. WARN={len(warnings)}", warnings)

    @activity.defn(name="create_clubs")
    async def create_clubs(self) -> ClubResult:
        self.calls.append(("create_clubs",))
        return ClubResult("test", ["arsenal"], 2, 1, [UNLINKED_TEAM])

    @activity.defn(name="resolve_identities")
    async def resolve_identities(self) -> IdentityResolutionResult:
        self.calls.append(("resolve_identities",))
        return IdentityResolutionResult("test", 2025, 1, 1, [], [UNLINKED_PLAYER], [])

    def worker(self, env: WorkflowEnvironment, task_queue: str) -> Worker:
        return Worker(
            env.client,
            task_queue=task_queue,
            workflows=[TransformWorkflow],
            activities=[self.build_dbt, self.create_clubs, self.resolve_identities],
        )


async def test_the_steps_run_in_order_with_their_dbt_selections(
    temporal: WorkflowEnvironment,
) -> None:
    stubs = Stubs()
    task_queue = f"test-{uuid.uuid4()}"
    async with stubs.worker(temporal, task_queue):
        result = await temporal.client.execute_workflow(
            TransformWorkflow.run, id=f"transform-{uuid.uuid4()}", task_queue=task_queue
        )

    assert stubs.calls == [
        ("dbt", "--select", "staging", "--exclude", "source:identity+"),
        ("create_clubs",),
        ("dbt", "--select", "clubs", "matches"),
        ("resolve_identities",),
        ("dbt", "--exclude", "staging", "clubs", "matches"),
    ]
    assert [t.step for t in result.times] == [
        "dbt staging",
        "clubs",
        "dbt clubs and matches",
        "identity resolution",
        "dbt the rest",
    ]


async def test_a_second_start_joins_the_running_run(temporal: WorkflowEnvironment) -> None:
    stubs = Stubs()
    stubs.release.clear()
    task_queue = f"test-{uuid.uuid4()}"
    async with stubs.worker(temporal, task_queue):
        first = await start_transform(temporal.client, task_queue)
        while not stubs.calls:  # the run is in its first step
            await asyncio.sleep(0.05)
        second = await start_transform(temporal.client, task_queue)
        stubs.release.set()
        await second.handle.result()

    run_id = first.handle.result_run_id
    assert first.handle.id == second.handle.id == TRANSFORM_WORKFLOW_ID
    assert second.handle.result_run_id == run_id
    assert (await first.handle.describe()).status == WorkflowExecutionStatus.COMPLETED
    assert [call[0] for call in stubs.calls].count("create_clubs") == 1

    assert first.message(task_queue) == f"Started Transform Run {run_id}; waiting for it to finish"
    started = (await first.handle.describe()).start_time
    assert second.start_time == started
    assert second.message(task_queue) == (
        f"Joined Transform Run {run_id}, started {started:%Y-%m-%d %H:%M:%S} UTC;"
        " waiting for it to finish"
    )


async def test_joining_a_run_on_another_queue_says_whose_it_is(
    temporal: WorkflowEnvironment,
) -> None:
    stubs = Stubs()
    stubs.release.clear()
    live_queue, own_queue = f"live-{uuid.uuid4()}", f"wt-{uuid.uuid4()}"
    async with stubs.worker(temporal, live_queue):
        await start_transform(temporal.client, live_queue)
        while not stubs.calls:
            await asyncio.sleep(0.05)
        joined = await start_transform(temporal.client, own_queue)
        stubs.release.set()
        await joined.handle.result()

    assert joined.joined
    assert joined.task_queue == live_queue
    assert joined.message(own_queue).splitlines()[1] == (
        f"It runs on queue {live_queue}, not yours ({own_queue}):"
        " it writes to that worker's database"
    )


async def test_an_unlinked_team_or_player_never_fails_the_run(
    temporal: WorkflowEnvironment,
) -> None:
    stubs = Stubs()
    task_queue = f"test-{uuid.uuid4()}"
    async with stubs.worker(temporal, task_queue):
        result = await temporal.client.execute_workflow(
            TransformWorkflow.run, id=f"transform-{uuid.uuid4()}", task_queue=task_queue
        )

    assert result.clubs.unresolved == [UNLINKED_TEAM]
    assert result.identity.unlinked == [UNLINKED_PLAYER]
    assert result.the_rest.warnings == [LEFT_OUT]
