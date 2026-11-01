import itertools
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from football_data_ai_platform.fetch_attempts import FetchAttemptStore
from football_data_ai_platform.ingestion.activities import SofascoreActivities
from football_data_ai_platform.ingestion.models import (
    BackfillInput,
    BackfillResult,
    CompleteMatchesInput,
)
from football_data_ai_platform.ingestion.workflows import (
    BackfillWorkflow,
    MatchIngestWorkflow,
    SeasonBackfillWorkflow,
)
from football_data_ai_platform.raw_responses import RawResponseStore
from football_data_ai_platform.sources import sofascore
from football_data_ai_platform.sources.interface import SourceInterface, SourcePolicy
from football_data_ai_platform.sources.sofascore.api import (
    ChallengeError,
    UpstreamError,
    raise_for_failure,
)
from football_data_ai_platform.sources.sofascore.matches import fetch_match
from football_data_ai_platform.sources.sofascore.seasons import (
    fetch_finished_event_ids,
    fetch_seasons,
)
from tests.sofascore_stub import (
    EVENT_ID,
    PLAYED,
    SEASON_EVENT_IDS,
    SEASON_ID,
    SEASONS,
    TOURNAMENT_ID,
    SofascoreStub,
)

FAILING_EVENT_ID = 1004
# The season list, and the two pages of season 24/25's events.
LISTING_REQUESTS = 3


class FailingMatch(SofascoreStub):
    """Answers every request for Match 1004 with a 500."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(f"/api/v1/event/{FAILING_EVENT_ID}"):
            self.requests.append(request)
            return httpx.Response(500)
        return super().handle(request)


class ChallengedMatch(SofascoreStub):
    """Answers every request for one Match with Sofascore's bot challenge."""

    def __init__(self, event_id: int) -> None:
        super().__init__()
        self.event_id = event_id

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(f"/api/v1/event/{self.event_id}"):
            self.requests.append(request)
            return httpx.Response(403, json={"error": {"code": 403, "reason": "challenge"}})
        return super().handle(request)


def _match_requests(stub: SofascoreStub) -> list[httpx.Request]:
    return [r for r in stub.requests if r.url.path.startswith("/api/v1/event/")]


def _source(backend: Callable[[], httpx.AsyncBaseTransport], rate: float = 1000) -> SourceInterface:
    policy = SourcePolicy(requests_per_second=rate, breaker_failures=100)
    return SourceInterface(sofascore.SOURCE, policy, backend=backend)


async def _backfill(
    env: WorkflowEnvironment,
    database_url: str,
    source: SourceInterface,
    year: str | None = "24/25",
    per_player: bool = True,
    max_concurrent_matches: int = 3,
) -> BackfillResult:
    activities = SofascoreActivities(database_url, source)
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[BackfillWorkflow, SeasonBackfillWorkflow, MatchIngestWorkflow],
        activities=[
            activities.ingest_match,
            activities.tournament_seasons,
            activities.season_matches,
            activities.complete_matches,
        ],
    ):
        return await env.client.execute_workflow(
            BackfillWorkflow.run,
            BackfillInput(
                tournament_id=TOURNAMENT_ID,
                year=year,
                per_player=per_player,
                max_concurrent_matches=max_concurrent_matches,
            ),
            id=f"backfill-{uuid.uuid4()}",
            task_queue=task_queue,
        )


async def test_a_season_lists_each_finished_match_once_across_its_pages(
    connection: AsyncConnection[Any],
) -> None:
    transport = sofascore.recording_transport(
        SofascoreStub().transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )

    seasons = await fetch_seasons(transport, TOURNAMENT_ID)
    finished = await fetch_finished_event_ids(transport, TOURNAMENT_ID, SEASON_ID)
    # No Match played yet: the first page is a 404, which ends the list.
    not_started = await fetch_finished_event_ids(transport, TOURNAMENT_ID, SEASONS[0][0])

    assert [(season.id, season.year) for season in seasons] == list(SEASONS)
    assert finished == list(SEASON_EVENT_IDS)
    assert not_started == []


async def test_one_failing_match_leaves_the_others_completed_and_is_listed(
    time_skipping: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = FailingMatch()

    result = await _backfill(time_skipping, database_url, _source(stub.transport))

    (season,) = result.seasons
    assert season.season.year == "24/25"
    assert (season.matches, season.already_complete, season.ingested) == (4, 0, 3)
    assert season.failed_event_ids == [FAILING_EVENT_ID]
    cursor = await connection.execute(
        "SELECT count(DISTINCT split_part(url, '/', 7)) FROM raw.raw_responses"
        " WHERE url LIKE '%/api/v1/event/%' AND status_code = 200"
    )
    assert await cursor.fetchone() == (3,)


async def test_a_rerun_skips_complete_matches_without_requests(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = SofascoreStub()
    source = _source(stub.transport)
    await _backfill(temporal, database_url, source)
    sent = len(stub.requests)

    rerun = await _backfill(temporal, database_url, source)

    # Within the Freshness Window even the event pages are reused: nothing reaches Sofascore.
    assert len(stub.requests) == sent
    (season,) = rerun.seasons
    assert (season.already_complete, season.ingested, season.raw_responses) == (4, 0, 0)

    # Past it, the season list and event pages are fetched again, but complete Matches still
    # are not: finished Matches do not change.
    await connection.execute(
        "UPDATE raw.raw_responses SET captured_at = captured_at - interval '25 hours'"
    )
    matches_before = len(_match_requests(stub))
    late = await _backfill(temporal, database_url, source)

    assert len(stub.requests) == sent + LISTING_REQUESTS
    assert len(_match_requests(stub)) == matches_before
    assert late.seasons[0].already_complete == 4


async def test_the_backfill_covers_the_latest_three_seasons(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    result = await _backfill(temporal, database_url, _source(SofascoreStub().transport), None)

    assert [season.season.year for season in result.seasons] == ["26/27", "25/26", "24/25"]
    assert [season.matches for season in result.seasons] == [0, 1, 4]
    # Match 1001 is listed in two seasons; the second season finds it complete.
    assert [season.already_complete for season in result.seasons] == [0, 0, 1]


async def test_matches_in_flight_share_the_rate_limit(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = SofascoreStub()
    sent_at: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent_at.append(time.monotonic())
        return stub.handle(request)

    rate = 50.0
    await _backfill(temporal, database_url, _source(lambda: httpx.MockTransport(handle), rate=rate))

    gaps = [later - earlier for earlier, later in itertools.pairwise(sent_at)]
    assert len(_match_requests(stub)) > 0
    assert min(gaps) >= 1 / rate * 0.95


class BlockedShotmap(SofascoreStub):
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/shotmap"):
            self.requests.append(request)
            return httpx.Response(403)
        return super().handle(request)


async def test_a_match_whose_latest_answer_was_a_failure_is_incomplete(
    database_url: str, connection: AsyncConnection[Any]
) -> None:
    store, attempts = RawResponseStore(connection), FetchAttemptStore(connection)
    activities = SofascoreActivities(database_url, _source(SofascoreStub().transport))

    async def complete() -> list[int]:
        return await ActivityEnvironment().run(
            activities.complete_matches, CompleteMatchesInput([EVENT_ID])
        )

    with pytest.raises(UpstreamError):
        await fetch_match(
            sofascore.recording_transport(BlockedShotmap().transport(), store, attempts),
            EVENT_ID,
        )
    assert await complete() == []

    await fetch_match(
        sofascore.recording_transport(SofascoreStub().transport(), store, attempts), EVENT_ID
    )
    assert await complete() == [EVENT_ID]


def _player_requests(stub: SofascoreStub) -> list[httpx.Request]:
    return [r for r in stub.requests if "/player/" in r.url.path]


async def test_a_match_level_pass_then_a_full_pass_fetch_each_response_once(
    temporal: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = SofascoreStub()
    source = _source(stub.transport)

    first = await _backfill(temporal, database_url, source, per_player=False)

    # Match-level: the event, lineups, shotmap and average positions; nothing per player.
    assert not first.per_player
    assert first.seasons[0].ingested == len(SEASON_EVENT_IDS)
    assert len(_match_requests(stub)) == 4 * len(SEASON_EVENT_IDS)
    assert _player_requests(stub) == []

    sent = len(stub.requests)
    rerun = await _backfill(temporal, database_url, source, per_player=False)
    assert len(stub.requests) == sent  # complete at Match level: no request at all
    assert rerun.seasons[0].already_complete == len(SEASON_EVENT_IDS)

    match_level = len(_match_requests(stub)) - len(_player_requests(stub))
    full = await _backfill(temporal, database_url, source)

    # The full pass adds only the per-player requests; the Match-level ones are reused.
    assert full.per_player
    assert full.seasons[0].already_complete == 0
    assert len(_match_requests(stub)) - len(_player_requests(stub)) == match_level
    assert len(_player_requests(stub)) == 2 * len(PLAYED) * len(SEASON_EVENT_IDS)


def test_only_a_403_saying_challenge_is_the_challenge() -> None:
    request = httpx.Request("GET", "https://www.sofascore.com/api/v1/event/1")
    challenge = {"error": {"code": 403, "reason": "challenge"}}

    with pytest.raises(ChallengeError):
        raise_for_failure(httpx.Response(403, json=challenge, request=request))
    for other in (
        httpx.Response(403, request=request),
        httpx.Response(429, json=challenge, request=request),
    ):
        with pytest.raises(UpstreamError) as error:
            raise_for_failure(other)
        assert not isinstance(error.value, ChallengeError)


async def test_a_challenge_stops_the_season_without_retries(
    time_skipping: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    stub = ChallengedMatch(FAILING_EVENT_ID)

    result = await _backfill(
        time_skipping, database_url, _source(stub.transport), max_concurrent_matches=1
    )

    (season,) = result.seasons
    # 1005 went first; 1004 was challenged and failed; 1003 and 1001 were never started.
    assert (season.ingested, season.failed_event_ids) == (1, [FAILING_EVENT_ID])
    assert season.challenged
    assert season.not_attempted_event_ids == [1003, 1001]
    challenged = [
        r for r in stub.requests if r.url.path.startswith(f"/api/v1/event/{FAILING_EVENT_ID}")
    ]
    assert len(challenged) == 1  # not retried
    assert {r.url.path.split("/")[4] for r in _match_requests(stub)} == {
        "1005",
        str(FAILING_EVENT_ID),
    }


async def test_a_challenged_season_stops_the_backfill(
    time_skipping: WorkflowEnvironment, database_url: str, connection: AsyncConnection[Any]
) -> None:
    # Newest first: 26/27 has no Match played; 25/26's only Match is challenged; 24/25 never runs.
    result = await _backfill(
        time_skipping, database_url, _source(ChallengedMatch(EVENT_ID).transport), year=None
    )

    assert [season.season.year for season in result.seasons] == ["26/27", "25/26"]
    assert result.seasons[-1].challenged
