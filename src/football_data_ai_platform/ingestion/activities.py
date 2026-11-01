"""Activities: all I/O of the ingestion workflows (HTTP, database) happens here."""

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta

import httpx
from psycopg import AsyncConnection
from temporalio import activity
from temporalio.exceptions import ApplicationError

from football_data_ai_platform.fetch_attempts import FetchAttemptStore
from football_data_ai_platform.ingestion.models import (
    CompetitionClubsInput,
    CompetitionClubsResult,
    CompleteMatchesInput,
    FixtureRef,
    FixturesInput,
    MatchIngestInput,
    MatchIngestResult,
    MatchStatusResult,
    SeasonMatchesInput,
    SeasonMatchesResult,
    SeasonRef,
    SeasonsInput,
    SquadIngestInput,
    SquadIngestResult,
    UnderstatSeasonInput,
    UnderstatSeasonResult,
)
from football_data_ai_platform.raw_responses import RawResponseStore
from football_data_ai_platform.sources import sofascore, transfermarkt, understat
from football_data_ai_platform.sources.interface import CircuitOpenError, SourceInterface
from football_data_ai_platform.sources.recording import RawResponseMissingError, RecordingTransport
from football_data_ai_platform.sources.sofascore.api import UpstreamError
from football_data_ai_platform.sources.sofascore.matches import (
    MatchNotFinished,
    MatchNotFound,
    fetch_match,
    fetch_match_status,
)
from football_data_ai_platform.sources.sofascore.seasons import (
    SeasonNotFound,
    fetch_finished_event_ids,
    fetch_seasons,
    fetch_upcoming_fixtures,
)
from football_data_ai_platform.sources.transfermarkt.competitions import fetch_competition_clubs
from football_data_ai_platform.sources.transfermarkt.squads import fetch_squad
from football_data_ai_platform.sources.understat import api as understat_api
from football_data_ai_platform.sources.understat import league as understat_league


class TransfermarktActivities:
    """Transfermarkt activities. `source` is the worker's one Transfermarkt source interface."""

    def __init__(self, database_url: str, source: SourceInterface) -> None:
        self._database_url = database_url
        self._source = source

    @activity.defn
    async def ingest_club_squad(self, input: SquadIngestInput) -> SquadIngestResult:
        """Fetch a squad and store every upstream response as a Raw Response."""
        async with self._recording(input.refresh) as transport:
            squad = await fetch_squad(transport, input.club_id, input.season_id)
        activity.logger.info(
            "Club %s season %s: %d players; %d Raw Responses fetched, %d reused",
            input.club_id,
            input.season_id,
            len(squad.players),
            transport.recorded,
            transport.reused,
        )
        return SquadIngestResult(
            club_id=input.club_id,
            season_id=input.season_id,
            players=len(squad.players),
            raw_responses=transport.recorded,
            reused=transport.reused,
        )

    @activity.defn
    async def ingest_competition_clubs(
        self, input: CompetitionClubsInput
    ) -> CompetitionClubsResult:
        """Fetch a competition season's clubs; store every upstream response as a Raw Response."""
        async with self._recording(input.refresh) as transport:
            competition = await fetch_competition_clubs(
                transport, input.competition_id, input.season_id
            )
        club_ids = [club.id for club in competition.clubs]
        activity.logger.info(
            "Competition %s season %s: %d clubs; %d Raw Responses fetched, %d reused",
            input.competition_id,
            input.season_id,
            len(club_ids),
            transport.recorded,
            transport.reused,
        )
        return CompetitionClubsResult(
            competition_id=input.competition_id,
            season_id=input.season_id,
            club_ids=club_ids,
            raw_responses=transport.recorded,
            reused=transport.reused,
        )

    def _recording(self, refresh: bool) -> AbstractAsyncContextManager[RecordingTransport]:
        return _recording(
            self._database_url, self._source, transfermarkt.recording_transport, refresh
        )


class SofascoreActivities:
    """Sofascore activities. `source` is the worker's one Sofascore source interface."""

    def __init__(self, database_url: str, source: SourceInterface) -> None:
        self._database_url = database_url
        self._source = source

    @activity.defn
    async def ingest_match(self, input: MatchIngestInput) -> MatchIngestResult:
        """Fetch one finished Match and store every response as a Raw Response."""
        async with self._recording(input.refresh) as transport:
            match = await fetch_match(_Heartbeat(transport), input.event_id, input.per_player)
        activity.logger.info(
            "Match %s (%s): %d players; %d Raw Responses fetched, %d reused",
            input.event_id,
            match.name,
            len(match.player_ids),
            transport.recorded,
            transport.reused,
        )
        return MatchIngestResult(
            event_id=input.event_id,
            name=match.name,
            players=len(match.player_ids),
            raw_responses=transport.recorded,
            reused=transport.reused,
        )

    @activity.defn
    async def tournament_seasons(self, input: SeasonsInput) -> list[SeasonRef]:
        """The tournament's latest `count` seasons, newest first, or the one with `year`."""
        async with self._recording(input.refresh) as transport:
            seasons = await fetch_seasons(transport, input.tournament_id)
        if input.year is None:
            return [SeasonRef(season.id, season.year) for season in seasons[: input.count]]
        for season in seasons:
            if season.year == input.year:
                return [SeasonRef(season.id, season.year)]
        raise SeasonNotFound(f"Tournament {input.tournament_id} has no season {input.year}")

    @activity.defn
    async def season_matches(self, input: SeasonMatchesInput) -> SeasonMatchesResult:
        """A season's finished Matches; its event pages are stored as Raw Responses."""
        async with self._recording(input.refresh) as transport:
            event_ids = await fetch_finished_event_ids(
                transport, input.tournament_id, input.season_id
            )
        return SeasonMatchesResult(event_ids, transport.recorded, transport.reused)

    @activity.defn
    async def complete_matches(self, input: CompleteMatchesInput) -> list[int]:
        """The Matches that replay from stored Raw Responses at the input's level, so need no
        request.

        Finished Matches do not change: completeness, not the Freshness Window, decides
        whether one is fetched again. A missing Raw Response or a stored failure (the latest
        answer to a request was a 403, say) makes a Match incomplete.
        """
        complete: list[int] = []
        async with await AsyncConnection.connect(self._database_url, autocommit=True) as conn:
            replay = sofascore.replay_transport(RawResponseStore(conn))
            for event_id in input.event_ids:
                activity.heartbeat(event_id)
                try:
                    await fetch_match(replay, event_id, input.per_player)
                except (
                    RawResponseMissingError,
                    UpstreamError,
                    MatchNotFound,
                    MatchNotFinished,
                ):
                    continue
                complete.append(event_id)
        return complete

    @activity.defn
    async def upcoming_fixtures(self, input: FixturesInput) -> list[FixtureRef]:
        """The current season's next fixtures, soonest first.

        Always fetched: the Freshness Window would answer with yesterday's list.
        """
        # One transport per call: closing a client closes its transport.
        async with self._recording(refresh=True) as transport:
            seasons = await fetch_seasons(transport, input.tournament_id)
        if not seasons:
            return []
        async with self._recording(refresh=True) as transport:
            fixtures = await fetch_upcoming_fixtures(transport, input.tournament_id, seasons[0].id)
        return [FixtureRef(f.event_id, f.start_timestamp) for f in fixtures]

    @activity.defn
    async def match_status(self, event_id: int) -> MatchStatusResult:
        """A Match's status and current kickoff.

        Always fetched: a stored event from an earlier check, still inside the Freshness
        Window, would report the Match as not started.
        """
        async with self._recording(refresh=True) as transport:
            status = await fetch_match_status(transport, event_id)
        return MatchStatusResult(status.status, status.start_timestamp)

    def _recording(self, refresh: bool) -> AbstractAsyncContextManager[RecordingTransport]:
        return _recording(self._database_url, self._source, sofascore.recording_transport, refresh)


class UnderstatActivities:
    """Understat activities. `source` is the worker's one Understat source interface."""

    def __init__(self, database_url: str, source: SourceInterface) -> None:
        self._database_url = database_url
        self._source = source

    @activity.defn
    async def ingest_season(self, input: UnderstatSeasonInput) -> UnderstatSeasonResult:
        """Store a league season's data, then every played Match not stored yet.

        The league data is always fetched: it changes as Matches are played. A played Match
        does not change, so a stored answer for it decides, not the Freshness Window. A Match
        that fails is listed and the others go on; a retry of the activity only fetches what
        is still missing.
        """
        async with self._recording(refresh=True) as transport:
            season = await understat_league.fetch_league_season(
                _Heartbeat(transport), input.league, input.year
            )
            raw_responses = transport.recorded
        complete: set[int] = (
            set() if input.refresh else await self._complete(season.finished_match_ids)
        )
        pending = [m for m in season.finished_match_ids if m not in complete]
        failed: list[int] = []
        async with self._recording(refresh=True) as transport:
            for match_id in pending:
                try:
                    await understat_league.fetch_match(_Heartbeat(transport), match_id)
                except (understat_league.MatchDataMissing, understat_api.UpstreamError) as error:
                    activity.logger.warning("Understat Match %s failed: %s", match_id, error)
                    failed.append(match_id)
            raw_responses += transport.recorded
        activity.logger.info(
            "Understat %s %s: %d played Matches, %d already stored, %d fetched, %d failed",
            input.league,
            input.year,
            len(season.finished_match_ids),
            len(complete),
            len(pending) - len(failed),
            len(failed),
        )
        return UnderstatSeasonResult(
            league=input.league,
            year=input.year,
            matches=len(season.finished_match_ids),
            already_complete=len(complete),
            ingested=len(pending) - len(failed),
            failed_match_ids=failed,
            raw_responses=raw_responses,
        )

    async def _complete(self, match_ids: Sequence[int]) -> set[int]:
        """The Matches whose latest stored answer is their data."""
        complete: set[int] = set()
        async with await AsyncConnection.connect(self._database_url, autocommit=True) as conn:
            replay = understat.replay_transport(RawResponseStore(conn))
            for match_id in match_ids:
                try:
                    await understat_league.fetch_match(replay, match_id)
                except (
                    RawResponseMissingError,
                    understat_league.MatchDataMissing,
                    understat_api.UpstreamError,
                ):
                    continue
                complete.add(match_id)
        return complete

    def _recording(self, refresh: bool) -> AbstractAsyncContextManager[RecordingTransport]:
        return _recording(self._database_url, self._source, understat.recording_transport, refresh)


class _Heartbeat(httpx.AsyncBaseTransport):
    """Heartbeat on every request, so a worker that died mid-Match is noticed within the
    activity's heartbeat timeout rather than its whole start-to-close timeout."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        activity.heartbeat()
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


RecordingFactory = Callable[
    [httpx.AsyncBaseTransport, RawResponseStore, FetchAttemptStore, timedelta | None],
    RecordingTransport,
]


@asynccontextmanager
async def _recording(
    database_url: str, source: SourceInterface, recording: RecordingFactory, refresh: bool
) -> AsyncGenerator[RecordingTransport]:
    """A recording transport over the source interface, for one activity attempt."""
    async with await AsyncConnection.connect(database_url, autocommit=True) as conn:
        freshness = None if refresh else source.freshness
        try:
            yield recording(
                source.transport(), RawResponseStore(conn), FetchAttemptStore(conn), freshness
            )
        except CircuitOpenError as error:
            raise _retry_after_cooldown(error) from error


def _retry_after_cooldown(error: CircuitOpenError) -> ApplicationError:
    """Fail the attempt, and schedule the next one for when the circuit breaker lets requests by."""
    return ApplicationError(
        str(error),
        type=type(error).__name__,
        next_retry_delay=timedelta(seconds=error.retry_after),
    )
