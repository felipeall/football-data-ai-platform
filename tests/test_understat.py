import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection
from temporalio.client import ScheduleCalendarSpec, ScheduleOverlapPolicy, WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from football_data_ai_platform.ingestion.activities import UnderstatActivities
from football_data_ai_platform.ingestion.models import UnderstatIngestInput, UnderstatIngestResult
from football_data_ai_platform.ingestion.schedules import ensure_understat_schedule
from football_data_ai_platform.ingestion.workflows import (
    UnderstatIngestWorkflow,
    current_understat_year,
)
from football_data_ai_platform.sources import understat
from football_data_ai_platform.sources.interface import SourceInterface, SourcePolicy
from tests.understat_stub import LEAGUE, MISSING_MATCH_ID, UnderstatStub


def _source(backend: Callable[[], httpx.AsyncBaseTransport]) -> SourceInterface:
    policy = SourcePolicy(requests_per_second=1000, breaker_failures=100)
    return SourceInterface(understat.SOURCE, policy, backend=backend)


async def _ingest(
    env: WorkflowEnvironment, database_url: str, source: SourceInterface, **input: Any
) -> UnderstatIngestResult:
    activities = UnderstatActivities(database_url, source)
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[UnderstatIngestWorkflow],
        activities=[activities.ingest_season],
    ):
        return await env.client.execute_workflow(
            UnderstatIngestWorkflow.run,
            UnderstatIngestInput(league=LEAGUE, **input),
            id=f"understat-{uuid.uuid4()}",
            task_queue=task_queue,
        )


async def test_a_season_stores_its_league_data_and_played_matches(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = UnderstatStub()

    result = await _ingest(temporal, database_url, _source(stub.transport), year=2024)

    (season,) = result.seasons
    assert (season.year, season.matches, season.already_complete, season.ingested) == (
        2024,
        2,
        0,
        1,
    )
    # Listed as played, but its data is a 404: a failure, not an absence of data.
    assert season.failed_match_ids == [MISSING_MATCH_ID]
    # Understat answers requests without the header 404, like a Match that does not exist.
    assert all(r.headers["X-Requested-With"] == "XMLHttpRequest" for r in stub.requests)
    cursor = await connection.execute(
        "SELECT url, status_code FROM raw.raw_responses WHERE source = 'understat' ORDER BY id"
    )
    assert await cursor.fetchall() == [
        ("https://understat.com/getLeagueData/EPL/2024", 200),
        ("https://understat.com/getMatchData/1", 200),
        ("https://understat.com/getMatchData/2", 404),
    ]


async def test_a_rerun_fetches_the_league_data_and_only_missing_matches(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = UnderstatStub()
    source = _source(stub.transport)
    await _ingest(temporal, database_url, source, year=2024)
    stub.requests.clear()

    rerun = await _ingest(temporal, database_url, source, year=2024)

    # The league data changes as Matches are played; Match 1 is stored, Match 2 is not.
    assert [r.url.path for r in stub.requests] == [
        "/getLeagueData/EPL/2024",
        f"/getMatchData/{MISSING_MATCH_ID}",
    ]
    (season,) = rerun.seasons
    assert (season.already_complete, season.ingested, season.failed_match_ids) == (1, 0, [2])


async def test_the_backfill_covers_the_current_season_and_the_two_before_it(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    current = current_understat_year(datetime.now(UTC))
    fixtures = {current - offset: [(10 + offset, True)] for offset in range(3)}

    result = await _ingest(temporal, database_url, _source(UnderstatStub(fixtures).transport))

    assert [(s.year, s.ingested) for s in result.seasons] == [
        (current, 1),
        (current - 1, 1),
        (current - 2, 1),
    ]


def test_the_current_season_starts_in_august() -> None:
    assert current_understat_year(datetime(2026, 7, 31, tzinfo=UTC)) == 2025
    assert current_understat_year(datetime(2026, 8, 1, tzinfo=UTC)) == 2026


async def test_a_season_understat_does_not_have_fails_without_retries(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = UnderstatStub()

    with pytest.raises(WorkflowFailureError):
        await _ingest(temporal, database_url, _source(stub.transport), year=2030)

    assert len(stub.requests) == 1


async def test_the_schedule_command_is_idempotent(temporal: WorkflowEnvironment) -> None:
    task_queue = f"test-{uuid.uuid4()}"

    outcomes = [await ensure_understat_schedule(temporal.client, task_queue) for _ in range(2)]

    handle = temporal.client.get_schedule_handle("understat-daily")
    schedule = (await handle.describe()).schedule
    await handle.delete()
    assert outcomes == ["created", "updated"]
    (calendar,) = schedule.spec.calendars
    assert isinstance(calendar, ScheduleCalendarSpec)
    assert [hour.start for hour in calendar.hour] == [7]
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.state.paused
