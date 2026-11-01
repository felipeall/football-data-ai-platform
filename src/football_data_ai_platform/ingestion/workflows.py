"""Workflows. Deterministic: no I/O here, only activity calls."""

import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ApplicationError,
    ChildWorkflowError,
    FailureError,
    WorkflowAlreadyStartedError,
)

with workflow.unsafe.imports_passed_through():
    from football_data_ai_platform.ingestion.activities import (
        SofascoreActivities,
        TransfermarktActivities,
        UnderstatActivities,
    )
    from football_data_ai_platform.ingestion.models import (
        BackfillInput,
        BackfillResult,
        CompetitionClubsInput,
        CompetitionSquadsInput,
        CompetitionSquadsResult,
        CompleteMatchesInput,
        FixturesInput,
        FixturesResult,
        MatchIngestInput,
        MatchIngestResult,
        PostMatchInput,
        PostMatchResult,
        SeasonBackfillInput,
        SeasonBackfillResult,
        SeasonMatchesInput,
        SeasonsInput,
        SquadIngestInput,
        SquadIngestResult,
        UnderstatIngestInput,
        UnderstatIngestResult,
        UnderstatSeasonInput,
        UnderstatSeasonResult,
    )
    from football_data_ai_platform.sources import sofascore, transfermarkt, understat

# Polite backoff: 10s, 20s, 40s, 80s between the five attempts. A failed attempt is retried
# whole; Raw Responses it stored stay, as a record of what the source answered.
TRANSFERMARKT_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
    non_retryable_error_types=list(transfermarkt.NON_RETRYABLE_ERRORS),
)
# The same backoff for Sofascore. A retried Match reuses the Raw Responses its failed attempts
# stored within the Freshness Window, so it only fetches what is still missing.
SOFASCORE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
    non_retryable_error_types=list(sofascore.NON_RETRYABLE_ERRORS),
)

# The same backoff for Understat. A retried season re-checks what is stored, so it only
# fetches the Matches still missing.
UNDERSTAT_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
    non_retryable_error_types=list(understat.NON_RETRYABLE_ERRORS),
)


@workflow.defn
class SquadIngestWorkflow:
    """Store one club's squad for one season as Raw Responses."""

    @workflow.run
    async def run(self, input: SquadIngestInput) -> SquadIngestResult:
        return await workflow.execute_activity_method(
            TransfermarktActivities.ingest_club_squad,
            input,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=TRANSFERMARKT_RETRY_POLICY,
        )


@workflow.defn
class CompetitionSquadsWorkflow:
    """Store every club's squad in one competition season as Raw Responses.

    One child `SquadIngestWorkflow` per club. A club that fails does not stop the others; the
    result lists it.
    """

    @workflow.run
    async def run(self, input: CompetitionSquadsInput) -> CompetitionSquadsResult:
        clubs = await workflow.execute_activity_method(
            TransfermarktActivities.ingest_competition_clubs,
            CompetitionClubsInput(input.competition_id, input.season_id, input.refresh),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=TRANSFERMARKT_RETRY_POLICY,
        )
        slots = asyncio.Semaphore(input.max_concurrent_clubs)

        async def squad(club_id: str) -> SquadIngestResult | None:
            async with slots:
                try:
                    return await workflow.execute_child_workflow(
                        SquadIngestWorkflow.run,
                        SquadIngestInput(club_id, input.season_id, input.refresh),
                        id=f"{workflow.info().workflow_id}-squad-{club_id}",
                    )
                except ChildWorkflowError:
                    workflow.logger.warning("Squad of club %s failed", club_id)
                    return None

        results = await asyncio.gather(*(squad(club_id) for club_id in clubs.club_ids))
        return CompetitionSquadsResult(
            competition_id=input.competition_id,
            season_id=input.season_id,
            clubs=clubs,
            squads=[result for result in results if result is not None],
            failed_club_ids=[
                club_id
                for club_id, result in zip(clubs.club_ids, results, strict=True)
                if result is None
            ],
        )


@workflow.defn
class MatchIngestWorkflow:
    """Store one finished Sofascore Match as Raw Responses."""

    @workflow.run
    async def run(self, input: MatchIngestInput) -> MatchIngestResult:
        # About 70 requests: a little over a minute at one per second, several when Matches
        # in flight share the rate limit. Heartbeats on every request catch a dead worker.
        return await workflow.execute_activity_method(
            SofascoreActivities.ingest_match,
            input,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=1),
            retry_policy=SOFASCORE_RETRY_POLICY,
        )


@workflow.defn
class SeasonBackfillWorkflow:
    """Store every finished Match of one season as Raw Responses.

    Matches already stored completely are skipped without a request. One child
    `MatchIngestWorkflow` per remaining Match; a Match that fails does not stop the others,
    and the result lists it.
    """

    @workflow.run
    async def run(self, input: SeasonBackfillInput) -> SeasonBackfillResult:
        listing = await workflow.execute_activity_method(
            SofascoreActivities.season_matches,
            SeasonMatchesInput(input.tournament_id, input.season.id, input.refresh),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=SOFASCORE_RETRY_POLICY,
        )
        complete: set[int] = set()
        if not input.refresh:
            complete = set(
                await workflow.execute_activity_method(
                    SofascoreActivities.complete_matches,
                    CompleteMatchesInput(listing.event_ids, input.per_player),
                    start_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=timedelta(minutes=1),
                    retry_policy=SOFASCORE_RETRY_POLICY,
                )
            )
        pending = [event_id for event_id in listing.event_ids if event_id not in complete]
        slots = asyncio.Semaphore(input.max_concurrent_matches)
        challenged = False
        not_attempted: list[int] = []

        async def match(event_id: int) -> MatchIngestResult | None:
            nonlocal challenged
            async with slots:
                # Once Sofascore challenges, every further request would be challenged too.
                if challenged:
                    not_attempted.append(event_id)
                    return None
                try:
                    return await workflow.execute_child_workflow(
                        MatchIngestWorkflow.run,
                        MatchIngestInput(event_id, input.refresh, input.per_player),
                        id=f"{workflow.info().workflow_id}-match-{event_id}",
                    )
                except ChildWorkflowError as error:
                    if _is_challenge(error):
                        workflow.logger.warning("Match %s challenged; stopping", event_id)
                        challenged = True
                    else:
                        workflow.logger.warning("Match %s failed", event_id)
                    return None

        results = await asyncio.gather(*(match(event_id) for event_id in pending))
        ingested = [result for result in results if result is not None]
        return SeasonBackfillResult(
            season=input.season,
            matches=len(listing.event_ids),
            already_complete=len(complete),
            ingested=len(ingested),
            failed_event_ids=[
                event_id
                for event_id, result in zip(pending, results, strict=True)
                if result is None and event_id not in not_attempted
            ],
            raw_responses=listing.raw_responses + sum(r.raw_responses for r in ingested),
            reused=listing.reused + sum(r.reused for r in ingested),
            challenged=challenged,
            not_attempted_event_ids=[e for e in pending if e in not_attempted],
        )


@workflow.defn
class BackfillWorkflow:
    """Store a tournament's latest seasons (ADR 5: the current one and the two before it), or
    one chosen season, as Raw Responses. One child `SeasonBackfillWorkflow` per season, one
    season after another."""

    @workflow.run
    async def run(self, input: BackfillInput) -> BackfillResult:
        seasons = await workflow.execute_activity_method(
            SofascoreActivities.tournament_seasons,
            SeasonsInput(input.tournament_id, input.count, input.year, input.refresh),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=SOFASCORE_RETRY_POLICY,
        )
        results: list[SeasonBackfillResult] = []
        for season in seasons:
            # A challenged season stopped early; the next would only be challenged too.
            if results and results[-1].challenged:
                break
            results.append(
                await workflow.execute_child_workflow(
                    SeasonBackfillWorkflow.run,
                    SeasonBackfillInput(
                        input.tournament_id,
                        season,
                        input.refresh,
                        input.max_concurrent_matches,
                        input.per_player,
                    ),
                    id=f"{workflow.info().workflow_id}-season-{season.id}",
                )
            )
        return BackfillResult(input.tournament_id, results, input.per_player)


def _is_challenge(error: BaseException | None) -> bool:
    """Whether a failure was caused, at any depth, by Sofascore's bot challenge."""
    while isinstance(error, FailureError):
        if isinstance(error, ApplicationError) and error.type == "ChallengeError":
            return True
        error = error.cause
    return False


# A normal Match ends about 1h55 after kickoff; Sofascore's post-match statistics settle over
# the next two hours (ADR 5: final whistle + 2h).
CHECK_AFTER_KICKOFF = timedelta(hours=4)
# A Match still on at that check (delayed, extra time…) is checked again this often, up to
# this many times.
LATE_CHECK_INTERVAL = timedelta(minutes=30)
MAX_LATE_CHECKS = 12
ENDED_WITHOUT_PLAY = frozenset({"postponed", "canceled", "cancelled", "abandoned"})


@workflow.defn
class PostMatchWorkflow:
    """Wait for one fixture to be played, then store it as Raw Responses.

    Sleeps on a durable timer until kickoff + 4h, then checks the Match: finished, it is
    ingested; not started yet with a new kickoff, the wait starts over from that kickoff;
    still on, it is checked again every 30 minutes, at most 12 times; postponed or
    cancelled, the workflow ends and a later fixtures run picks up the new date.
    """

    @workflow.run
    async def run(self, input: PostMatchInput) -> PostMatchResult:
        kickoff = input.start_timestamp
        check_at = kickoff + CHECK_AFTER_KICKOFF.total_seconds()
        late_checks = 0
        while True:
            delay = check_at - workflow.now().timestamp()
            if delay > 0:
                await workflow.sleep(delay)
            status = await workflow.execute_activity_method(
                SofascoreActivities.match_status,
                input.event_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=SOFASCORE_RETRY_POLICY,
            )
            if status.status == "finished":
                ingest = await workflow.execute_child_workflow(
                    MatchIngestWorkflow.run,
                    MatchIngestInput(input.event_id),
                    id=f"{workflow.info().workflow_id}-ingest",
                )
                return PostMatchResult(input.event_id, "ingested", ingest)
            if status.status in ENDED_WITHOUT_PLAY:
                return PostMatchResult(input.event_id, status.status)
            if status.status == "notstarted" and status.start_timestamp != kickoff:
                workflow.logger.info(
                    "Match %s moved: kickoff %s, was %s",
                    input.event_id,
                    status.start_timestamp,
                    kickoff,
                )
                kickoff = status.start_timestamp
                check_at = kickoff + CHECK_AFTER_KICKOFF.total_seconds()
                late_checks = 0
                continue
            late_checks += 1
            if late_checks > MAX_LATE_CHECKS:
                workflow.logger.warning(
                    "Match %s still %s after %d checks", input.event_id, status.status, late_checks
                )
                return PostMatchResult(input.event_id, "gave_up")
            check_at = workflow.now().timestamp() + LATE_CHECK_INTERVAL.total_seconds()


@workflow.defn
class FixturesWorkflow:
    """Start one `PostMatchWorkflow` per fixture kicking off within the horizon; run daily by
    a Schedule (ADR 5).

    Post-match workflows are keyed by Match (`post-match-<event id>`) and outlive this run, so
    a fixture read on several days starts one workflow.
    """

    @workflow.run
    async def run(self, input: FixturesInput) -> FixturesResult:
        fixtures = await workflow.execute_activity_method(
            SofascoreActivities.upcoming_fixtures,
            input,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=SOFASCORE_RETRY_POLICY,
        )
        horizon = workflow.now() + timedelta(days=input.horizon_days)
        started: list[int] = []
        already_scheduled: list[int] = []
        beyond_horizon = 0
        for fixture in fixtures:
            if fixture.start_timestamp > horizon.timestamp():
                beyond_horizon += 1
                continue
            try:
                await workflow.start_child_workflow(
                    PostMatchWorkflow.run,
                    PostMatchInput(fixture.event_id, fixture.start_timestamp),
                    id=f"post-match-{fixture.event_id}",
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
                started.append(fixture.event_id)
            except WorkflowAlreadyStartedError:
                already_scheduled.append(fixture.event_id)
        return FixturesResult(started, already_scheduled, beyond_horizon)


# Understat names a season by its start year. The Premier League starts in August, so until
# then the current season is the one that started the year before.
SEASON_START_MONTH = 8


def current_understat_year(now: datetime) -> int:
    """The start year of the season under way, or about to start, at `now`."""
    return now.year if now.month >= SEASON_START_MONTH else now.year - 1


@workflow.defn
class UnderstatIngestWorkflow:
    """Store a league's latest Understat seasons, ending with the current one, or one chosen
    season, as Raw Responses: one activity per season, one season after another.

    The daily Schedule runs it for the current season only; a backfill for three (ADR 5).
    """

    @workflow.run
    async def run(self, input: UnderstatIngestInput) -> UnderstatIngestResult:
        if input.year is not None:
            years = [input.year]
        else:
            current = current_understat_year(workflow.now())
            years = [current - offset for offset in range(input.count)]
        seasons: list[UnderstatSeasonResult] = []
        for year in years:
            # About 380 requests at the source's rate: some minutes. Heartbeats on every
            # request catch a dead worker.
            seasons.append(
                await workflow.execute_activity_method(
                    UnderstatActivities.ingest_season,
                    UnderstatSeasonInput(input.league, year, input.refresh),
                    start_to_close_timeout=timedelta(hours=1),
                    heartbeat_timeout=timedelta(minutes=1),
                    retry_policy=UNDERSTAT_RETRY_POLICY,
                )
            )
        return UnderstatIngestResult(input.league, seasons)
