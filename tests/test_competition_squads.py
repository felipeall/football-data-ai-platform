import itertools
import time
import uuid
from typing import Any

import httpx
from psycopg import AsyncConnection
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from football_data_ai_platform.ingestion.activities import TransfermarktActivities
from football_data_ai_platform.ingestion.models import (
    CompetitionSquadsInput,
    CompetitionSquadsResult,
)
from football_data_ai_platform.ingestion.workflows import (
    CompetitionSquadsWorkflow,
    SquadIngestWorkflow,
)
from football_data_ai_platform.sources.interface import SourceInterface, SourcePolicy
from football_data_ai_platform.sources.transfermarkt import SOURCE, is_upstream_failure
from tests.transfermarkt_stub import (
    COMPETITION_CLUB_IDS,
    COMPETITION_ID,
    FAILING_CLUB_ID,
    PLAYER_IDS,
    SEASON_ID,
    SQUAD_CLUB_IDS,
    TransfermarktStub,
)


class TimedStub(TransfermarktStub):
    """Also records when each request reached the source."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_at: list[float] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.sent_at.append(time.monotonic())
        return super().handle(request)


async def _run(
    env: WorkflowEnvironment, database_url: str, stub: TransfermarktStub, rate: float
) -> CompetitionSquadsResult:
    policy = SourcePolicy(requests_per_second=rate, breaker_failures=100)
    source = SourceInterface(SOURCE, policy, is_failure=is_upstream_failure, backend=stub.transport)
    activities = TransfermarktActivities(database_url, source)
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CompetitionSquadsWorkflow, SquadIngestWorkflow],
        activities=[activities.ingest_club_squad, activities.ingest_competition_clubs],
    ):
        return await env.client.execute_workflow(
            CompetitionSquadsWorkflow.run,
            CompetitionSquadsInput(competition_id=COMPETITION_ID, season_id=SEASON_ID),
            id=f"competition-squads-{uuid.uuid4()}",
            task_queue=task_queue,
        )


async def test_one_failing_club_leaves_the_others_completed(
    time_skipping: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = TransfermarktStub()

    result = await _run(time_skipping, database_url, stub, rate=1000)

    assert result.clubs.club_ids == list(COMPETITION_CLUB_IDS)
    assert result.failed_club_ids == [FAILING_CLUB_ID]
    assert sorted(squad.club_id for squad in result.squads) == sorted(SQUAD_CLUB_IDS)
    assert all(squad.players == len(PLAYER_IDS) for squad in result.squads)
    # The competition's clubs and every squad's responses are stored, the 404 included.
    cursor = await connection.execute("SELECT url FROM raw.raw_responses")
    stored = {url for (url,) in await cursor.fetchall()}
    assert any(f"/competition/{COMPETITION_ID}/club" in url for url in stored)
    for club_id in COMPETITION_CLUB_IDS:
        assert any(f"/club/{club_id}/squad" in url for url in stored)


async def test_fan_out_stays_within_the_rate_limit(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = TimedStub()
    rate = 100.0

    await _run(temporal, database_url, stub, rate=rate)

    # Squads run concurrently, yet every request across them is spaced by the one limiter.
    gaps = [later - earlier for earlier, later in itertools.pairwise(stub.sent_at)]
    assert len(stub.sent_at) > 10
    assert min(gaps) >= 1 / rate - 0.002
