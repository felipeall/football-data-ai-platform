import itertools
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from football_data_ai_platform.fetch_attempts import FetchAttemptStore
from football_data_ai_platform.ingestion.activities import SofascoreActivities
from football_data_ai_platform.ingestion.models import MatchIngestInput, MatchIngestResult
from football_data_ai_platform.ingestion.workflows import MatchIngestWorkflow
from football_data_ai_platform.raw_responses import RawResponseStore
from football_data_ai_platform.sources import sofascore
from football_data_ai_platform.sources.interface import (
    CircuitOpenError,
    SourceInterface,
    SourcePolicy,
)
from football_data_ai_platform.sources.recording import RawResponseMissingError
from football_data_ai_platform.sources.sofascore.api import UpstreamError
from football_data_ai_platform.sources.sofascore.matches import fetch_match
from tests.sofascore_stub import (
    BENCHED,
    EVENT_ID,
    NO_HEATMAP,
    NOT_STARTED_EVENT_ID,
    PLAYED,
    SofascoreStub,
)

# The event, lineups, shotmap and average positions, then a heatmap and a rating breakdown
# for each player who appeared.
REQUESTS_PER_MATCH = 4 + 2 * len(PLAYED)


def _recording(
    connection: AsyncConnection[Any], backend: httpx.AsyncBaseTransport
) -> httpx.AsyncBaseTransport:
    return sofascore.recording_transport(
        backend, RawResponseStore(connection), FetchAttemptStore(connection)
    )


async def test_a_match_is_stored_as_sofascore_raw_responses(
    connection: AsyncConnection[Any],
) -> None:
    stub = SofascoreStub()

    match = await fetch_match(_recording(connection, stub.transport()), EVENT_ID)

    assert match.name == "Home FC v Away FC"
    assert match.player_ids == PLAYED
    assert len(stub.requests) == REQUESTS_PER_MATCH
    # An unused substitute has no heatmap to ask for.
    assert not any(f"/player/{BENCHED}/" in str(r.url) for r in stub.requests)
    # A late substitute's missing heatmap is a stored answer, not a failure.
    assert match.statuses[f"/event/{EVENT_ID}/player/{NO_HEATMAP}/heatmap"] == 404
    cursor = await connection.execute(
        "SELECT source, count(*) FROM raw.raw_responses GROUP BY source"
    )
    assert await cursor.fetchall() == [("sofascore", REQUESTS_PER_MATCH)]


async def test_replay_reads_the_same_match_back_without_network(
    connection: AsyncConnection[Any],
) -> None:
    store = RawResponseStore(connection)
    live = await fetch_match(_recording(connection, SofascoreStub().transport()), EVENT_ID)

    # The replay transport has no backend at all: it can only answer from the store.
    replayed = await fetch_match(sofascore.replay_transport(store), EVENT_ID)

    assert replayed == live
    with pytest.raises(RawResponseMissingError):
        await fetch_match(sofascore.replay_transport(store), 9999)


async def test_sofascore_requests_stay_within_the_rate_limit(
    connection: AsyncConnection[Any],
) -> None:
    stub = SofascoreStub()
    sent_at: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent_at.append(time.monotonic())
        return stub.handle(request)

    rate = 50.0
    source = SourceInterface(
        sofascore.SOURCE,
        SourcePolicy(requests_per_second=rate),
        backend=lambda: httpx.MockTransport(handle),
    )

    await fetch_match(_recording(connection, source.transport()), EVENT_ID)

    gaps = [later - earlier for earlier, later in itertools.pairwise(sent_at)]
    assert len(sent_at) == REQUESTS_PER_MATCH
    assert min(gaps) >= 1 / rate * 0.95


async def test_the_circuit_breaker_stops_requests_to_a_blocking_sofascore(
    connection: AsyncConnection[Any],
) -> None:
    sent: list[httpx.Request] = []

    def blocked(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(403)

    source = SourceInterface(
        sofascore.SOURCE,
        SourcePolicy(requests_per_second=1000, breaker_failures=2),
        backend=lambda: httpx.MockTransport(blocked),
    )

    for _ in range(2):
        with pytest.raises(UpstreamError):
            await fetch_match(_recording(connection, source.transport()), EVENT_ID)
    with pytest.raises(CircuitOpenError):
        await fetch_match(_recording(connection, source.transport()), EVENT_ID)

    assert len(sent) == 2


class BlockedShotmap(SofascoreStub):
    """Answers the shotmap request with 403 `failures` times, then like the stub."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.shotmap_requests = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/event/{EVENT_ID}/shotmap":
            self.shotmap_requests += 1
            if self.shotmap_requests <= self.failures:
                self.requests.append(request)
                return httpx.Response(403)
        return super().handle(request)


async def _run(
    env: WorkflowEnvironment,
    database_url: str,
    backend: Callable[[], httpx.AsyncBaseTransport],
    event_id: int = EVENT_ID,
) -> MatchIngestResult:
    policy = SourcePolicy(requests_per_second=1000, breaker_failures=100)
    source = SourceInterface(sofascore.SOURCE, policy, backend=backend)
    activities = SofascoreActivities(database_url, source)
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[MatchIngestWorkflow],
        activities=[activities.ingest_match],
    ):
        return await env.client.execute_workflow(
            MatchIngestWorkflow.run,
            MatchIngestInput(event_id=event_id),
            id=f"match-{uuid.uuid4()}",
            task_queue=task_queue,
        )


async def test_a_missing_player_heatmap_leaves_the_match_ingest_successful(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = SofascoreStub()

    result = await _run(temporal, database_url, stub.transport)

    assert result == MatchIngestResult(
        event_id=EVENT_ID,
        name="Home FC v Away FC",
        players=len(PLAYED),
        raw_responses=REQUESTS_PER_MATCH,
        reused=0,
    )


async def test_a_blocked_match_is_retried_and_reuses_what_it_stored(
    time_skipping: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = BlockedShotmap(failures=2)

    result = await _run(time_skipping, database_url, stub.transport)

    assert stub.shotmap_requests == 3
    # The event and lineups were fetched once; the retries answered them from the store.
    assert len(stub.requests) == REQUESTS_PER_MATCH + 2
    assert (result.raw_responses, result.reused) == (REQUESTS_PER_MATCH - 2, 2)
    assert result.players == len(PLAYED)


@pytest.mark.parametrize(
    ("event_id", "error"),
    [
        pytest.param(9999, "MatchNotFound", id="unknown-event"),
        pytest.param(NOT_STARTED_EVENT_ID, "MatchNotFinished", id="not-finished"),
    ],
)
async def test_a_missing_or_unfinished_match_is_not_retried(
    time_skipping: WorkflowEnvironment,
    database_url: str,
    connection: AsyncConnection[Any],
    event_id: int,
    error: str,
) -> None:
    stub = SofascoreStub()

    with pytest.raises(WorkflowFailureError) as failure:
        await _run(time_skipping, database_url, stub.transport, event_id)

    activity_error = failure.value.cause
    assert isinstance(activity_error, ActivityError)
    assert isinstance(activity_error.cause, ApplicationError)
    assert activity_error.cause.type == error
    assert len(stub.requests) == 1
