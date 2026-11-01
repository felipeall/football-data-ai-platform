import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from football_data_ai_platform.ingestion.activities import TransfermarktActivities
from football_data_ai_platform.ingestion.models import SquadIngestInput, SquadIngestResult
from football_data_ai_platform.ingestion.workflows import (
    TRANSFERMARKT_RETRY_POLICY,
    SquadIngestWorkflow,
)
from football_data_ai_platform.sources.interface import SourceInterface, SourcePolicy
from football_data_ai_platform.sources.transfermarkt import SOURCE, is_upstream_failure
from tests.transfermarkt_stub import CLUB_ID, PLAYER_IDS, SEASON_ID, TransfermarktStub


def _source(backend: Callable[[], httpx.AsyncBaseTransport]) -> SourceInterface:
    policy = SourcePolicy(requests_per_second=1000, breaker_failures=100)
    return SourceInterface(SOURCE, policy, is_failure=is_upstream_failure, backend=backend)


async def test_squad_workflow_stores_raw_responses_end_to_end(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = TransfermarktStub()
    activities = TransfermarktActivities(database_url, _source(stub.transport))
    task_queue = f"test-{uuid.uuid4()}"

    async with Worker(
        temporal.client,
        task_queue=task_queue,
        workflows=[SquadIngestWorkflow],
        activities=[activities.ingest_club_squad],
    ):
        handle = await temporal.client.start_workflow(
            SquadIngestWorkflow.run,
            SquadIngestInput(club_id=CLUB_ID, season_id=SEASON_ID),
            id=f"squad-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        result = await handle.result()

    assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED
    assert result.players == len(PLAYER_IDS)
    assert result.raw_responses == len(stub.requests)
    cursor = await connection.execute(
        "SELECT count(*) FROM raw.raw_responses WHERE source = 'transfermarkt'"
    )
    assert await cursor.fetchone() == (len(stub.requests),)


class FlakySquad(TransfermarktStub):
    """Fails the squad request `failures` times with `fail`, then answers like the stub."""

    def __init__(self, failures: int, fail: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__()
        self.failures = failures
        self.fail = fail
        self.squad_requests = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/club/{CLUB_ID}/squad":
            self.squad_requests += 1
            if self.squad_requests <= self.failures:
                self.requests.append(request)
                return self.fail(request)
        return super().handle(request)


def _blocked(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403)


def _not_found(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


async def _run(
    env: WorkflowEnvironment, database_url: str, stub: TransfermarktStub, refresh: bool = False
) -> SquadIngestResult:
    activities = TransfermarktActivities(database_url, _source(stub.transport))
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[SquadIngestWorkflow],
        activities=[activities.ingest_club_squad],
    ):
        return await env.client.execute_workflow(
            SquadIngestWorkflow.run,
            SquadIngestInput(club_id=CLUB_ID, season_id=SEASON_ID, refresh=refresh),
            id=f"squad-{uuid.uuid4()}",
            task_queue=task_queue,
        )


@pytest.mark.parametrize(
    "fail",
    [
        pytest.param(_blocked, id="UpstreamBlocked"),
        pytest.param(_timeout, id="httpx-timeout"),
    ],
)
async def test_transient_failures_are_retried_with_backoff(
    time_skipping: WorkflowEnvironment,
    database_url: str,
    connection: AsyncConnection[Any],
    fail: Callable[[httpx.Request], httpx.Response],
) -> None:
    stub = FlakySquad(failures=2, fail=fail)

    result = await _run(time_skipping, database_url, stub)

    assert result.players == len(PLAYER_IDS)
    assert stub.squad_requests == 3
    assert TRANSFERMARKT_RETRY_POLICY.backoff_coefficient > 1


async def test_not_found_is_not_retried(
    time_skipping: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = FlakySquad(failures=5, fail=_not_found)

    with pytest.raises(WorkflowFailureError) as failure:
        await _run(time_skipping, database_url, stub)

    activity_error = failure.value.cause
    assert isinstance(activity_error, ActivityError)
    assert isinstance(activity_error.cause, ApplicationError)
    assert activity_error.cause.type == "NotFound"
    assert stub.squad_requests == 1


async def _stored(connection: AsyncConnection[Any]) -> int:
    cursor = await connection.execute("SELECT count(*) FROM raw.raw_responses")
    (count,) = await cursor.fetchone() or (0,)
    return count


async def test_second_run_inside_the_freshness_window_reuses_raw_responses(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = TransfermarktStub()
    first = await _run(temporal, database_url, stub)
    fetched = len(stub.requests)

    second = await _run(temporal, database_url, stub)

    assert len(stub.requests) == fetched  # No request reached the source.
    assert not first.cache_hit
    assert (first.raw_responses, first.reused) == (fetched, 0)
    assert second.cache_hit
    assert (second.raw_responses, second.reused) == (0, fetched)
    assert second.players == first.players == len(PLAYER_IDS)
    assert await _stored(connection) == fetched


async def test_run_after_the_freshness_window_fetches_again_and_keeps_old_raw_responses(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = TransfermarktStub()
    await _run(temporal, database_url, stub)
    fetched = len(stub.requests)
    await connection.execute(
        "UPDATE raw.raw_responses SET captured_at = captured_at - interval '25 hours'"
    )

    result = await _run(temporal, database_url, stub)

    assert len(stub.requests) == 2 * fetched
    assert (result.raw_responses, result.reused) == (fetched, 0)
    assert await _stored(connection) == 2 * fetched


async def test_refresh_skips_fresh_raw_responses(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = TransfermarktStub()
    await _run(temporal, database_url, stub)
    fetched = len(stub.requests)

    result = await _run(temporal, database_url, stub, refresh=True)

    assert len(stub.requests) == 2 * fetched
    assert not result.cache_hit
    assert result.reused == 0
    assert await _stored(connection) == 2 * fetched
