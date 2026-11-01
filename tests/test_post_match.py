import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from psycopg import AsyncConnection
from temporalio.client import ScheduleCalendarSpec, ScheduleOverlapPolicy, ScheduleState
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from football_data_ai_platform.ingestion.activities import SofascoreActivities
from football_data_ai_platform.ingestion.models import (
    FixturesInput,
    FixturesResult,
    PostMatchInput,
    PostMatchResult,
)
from football_data_ai_platform.ingestion.schedules import (
    ensure_post_match_schedule,
    post_match_schedule,
)
from football_data_ai_platform.ingestion.workflows import (
    FixturesWorkflow,
    MatchIngestWorkflow,
    PostMatchWorkflow,
)
from football_data_ai_platform.sources import sofascore
from football_data_ai_platform.sources.interface import SourceInterface, SourcePolicy
from tests.sofascore_stub import EVENT_ID, PLAYED, SEASONS, TOURNAMENT_ID, SofascoreStub

EVENT_PATH = f"/api/v1/event/{EVENT_ID}"


class LiveMatch(SofascoreStub):
    """Match 1001 as it unfolds: each status check gets the next `(status, kickoff)`; the last
    one sticks."""

    def __init__(self, timeline: list[tuple[str, int]]) -> None:
        super().__init__()
        self.timeline = timeline
        self.status_checks = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != EVENT_PATH:
            return super().handle(request)
        self.requests.append(request)
        status, kickoff = self.timeline[min(self.status_checks, len(self.timeline) - 1)]
        self.status_checks += 1
        event = {
            "id": EVENT_ID,
            "status": {"type": status},
            "startTimestamp": kickoff,
            "homeTeam": {"id": 1, "name": "Home FC"},
            "awayTeam": {"id": 2, "name": "Away FC"},
        }
        return httpx.Response(200, json={"event": event})


class Fixtures(SofascoreStub):
    """The current season (26/27) lists `fixtures` as its next Matches."""

    def __init__(self, fixtures: list[tuple[int, int]]) -> None:
        super().__init__()
        self.fixtures = fixtures

    def handle(self, request: httpx.Request) -> httpx.Response:
        season_id = SEASONS[0][0]
        path = f"/api/v1/unique-tournament/{TOURNAMENT_ID}/season/{season_id}/events/next/0"
        if request.url.path != path:
            return super().handle(request)
        self.requests.append(request)
        events = [
            {"id": event_id, "startTimestamp": kickoff, "status": {"type": "notstarted"}}
            for event_id, kickoff in self.fixtures
        ]
        return httpx.Response(200, json={"events": events, "hasNextPage": True})


def _worker(
    env: WorkflowEnvironment, database_url: str, backend: Callable[[], httpx.AsyncBaseTransport]
) -> tuple[Worker, str]:
    policy = SourcePolicy(requests_per_second=1000, breaker_failures=100)
    activities = SofascoreActivities(
        database_url, SourceInterface(sofascore.SOURCE, policy, backend=backend)
    )
    task_queue = f"test-{uuid.uuid4()}"
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[FixturesWorkflow, PostMatchWorkflow, MatchIngestWorkflow],
        activities=[
            activities.upcoming_fixtures,
            activities.match_status,
            activities.ingest_match,
        ],
    )
    return worker, task_queue


async def _post_match(
    env: WorkflowEnvironment, database_url: str, stub: LiveMatch, kickoff: int
) -> PostMatchResult:
    worker, task_queue = _worker(env, database_url, stub.transport)
    async with worker:
        return await env.client.execute_workflow(
            PostMatchWorkflow.run,
            PostMatchInput(EVENT_ID, kickoff),
            id=f"post-match-{uuid.uuid4()}",
            task_queue=task_queue,
        )


async def _kickoff_in(env: WorkflowEnvironment, delay: timedelta) -> int:
    return int((await env.get_current_time() + delay).timestamp())


async def test_a_finished_match_is_ingested_after_its_timer(
    time_skipping: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    kickoff = await _kickoff_in(time_skipping, timedelta(hours=1))
    stub = LiveMatch([("finished", kickoff)])

    result = await _post_match(time_skipping, database_url, stub, kickoff)

    assert result.outcome == "ingested"
    assert result.ingest is not None
    assert result.ingest.players == len(PLAYED)
    # In full: per-player heatmaps and rating breakdowns included.
    assert sum("/player/" in r.url.path for r in stub.requests) == 2 * len(PLAYED)
    # One status check; the ingest then reused the finished event it stored.
    assert stub.status_checks == 1
    # The timer ran to kickoff + 4h before that check.
    assert await time_skipping.get_current_time() >= _at(kickoff + 4 * 3600)


async def test_a_late_finishing_match_is_checked_again_then_ingested(
    time_skipping: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    kickoff = await _kickoff_in(time_skipping, timedelta(hours=1))
    stub = LiveMatch([("inprogress", kickoff), ("inprogress", kickoff), ("finished", kickoff)])

    result = await _post_match(time_skipping, database_url, stub, kickoff)

    assert result.outcome == "ingested"
    assert stub.status_checks == 3


async def test_a_moved_kickoff_restarts_the_wait(
    time_skipping: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    kickoff = await _kickoff_in(time_skipping, timedelta(hours=1))
    moved = kickoff + 3 * 24 * 3600
    stub = LiveMatch([("notstarted", moved), ("finished", moved)])

    result = await _post_match(time_skipping, database_url, stub, kickoff)

    assert result.outcome == "ingested"
    assert stub.status_checks == 2
    assert await time_skipping.get_current_time() >= _at(moved + 4 * 3600)


async def test_a_postponed_match_ends_without_ingesting(
    time_skipping: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    kickoff = await _kickoff_in(time_skipping, timedelta(hours=1))
    stub = LiveMatch([("postponed", kickoff)])

    result = await _post_match(time_skipping, database_url, stub, kickoff)

    assert result == PostMatchResult(EVENT_ID, "postponed")
    assert [r.url.path for r in stub.requests] == [EVENT_PATH]


async def test_a_fixture_read_twice_starts_one_post_match_workflow(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    # Unique per run: post-match workflow IDs are keyed by Match on the shared test server.
    soon, later = 900_000 + uuid.uuid4().int % 100_000, 800_000
    stub = Fixtures(
        [
            (soon, await _kickoff_in(temporal, timedelta(days=1))),
            (later, await _kickoff_in(temporal, timedelta(days=20))),
        ]
    )
    worker, task_queue = _worker(temporal, database_url, stub.transport)

    async def read_fixtures() -> FixturesResult:
        return await temporal.client.execute_workflow(
            FixturesWorkflow.run,
            FixturesInput(TOURNAMENT_ID),
            id=f"fixtures-{uuid.uuid4()}",
            task_queue=task_queue,
        )

    async with worker:
        first = await read_fixtures()
        second = await read_fixtures()
        post_match = temporal.client.get_workflow_handle(f"post-match-{soon}")
        running = (await post_match.describe()).status
        await post_match.terminate("test done")

    assert first == FixturesResult(started=[soon], already_scheduled=[], beyond_horizon=1)
    assert second == FixturesResult(started=[], already_scheduled=[soon], beyond_horizon=1)
    assert running is not None and running.name == "RUNNING"


async def test_the_schedule_command_is_idempotent(temporal: WorkflowEnvironment) -> None:
    task_queue = f"test-{uuid.uuid4()}"

    outcomes = [await ensure_post_match_schedule(temporal.client, task_queue) for _ in range(2)]

    handle = temporal.client.get_schedule_handle("post-match-fixtures")
    schedule = (await handle.describe()).schedule
    await handle.delete()
    assert outcomes == ["created", "updated"]
    (calendar,) = schedule.spec.calendars
    assert isinstance(calendar, ScheduleCalendarSpec)
    assert [hour.start for hour in calendar.hour] == [6]
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.state.paused


async def test_the_schedule_command_pauses_a_running_schedule(
    temporal: WorkflowEnvironment,
) -> None:
    task_queue = f"test-{uuid.uuid4()}"
    running = post_match_schedule(task_queue)
    running.state = ScheduleState(paused=False)
    await temporal.client.create_schedule("post-match-fixtures", running)

    outcome = await ensure_post_match_schedule(temporal.client, task_queue)

    handle = temporal.client.get_schedule_handle("post-match-fixtures")
    schedule = (await handle.describe()).schedule
    await handle.delete()
    assert outcome == "updated"
    assert schedule.state.paused


def _at(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)
