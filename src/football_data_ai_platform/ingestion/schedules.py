"""Temporal Schedules: the post-match refresh cadence (ADR 5) and Understat's daily refresh."""

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleCalendarSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)

from football_data_ai_platform.ingestion.models import FixturesInput, UnderstatIngestInput
from football_data_ai_platform.ingestion.workflows import FixturesWorkflow, UnderstatIngestWorkflow
from football_data_ai_platform.sources.sofascore.seasons import PREMIER_LEAGUE
from football_data_ai_platform.sources.understat.league import (
    PREMIER_LEAGUE as UNDERSTAT_PREMIER_LEAGUE,
)

POST_MATCH_SCHEDULE_ID = "post-match-fixtures"
RUN_AT_HOUR_UTC = 6
UNDERSTAT_SCHEDULE_ID = "understat-daily"
# After the night's Matches: Understat has their data by morning.
UNDERSTAT_RUN_AT_HOUR_UTC = 7
# Ingestion runs by hand while the stack is local; a Schedule is created paused, and
# `temporal schedule toggle --unpause` starts it.
PAUSED = ScheduleState(paused=True, note="Paused: ingestion runs by hand while the stack is local")


def post_match_schedule(task_queue: str) -> Schedule:
    """Daily at 06:00 UTC, read the Premier League's fixtures and start their post-match
    workflows. A run still going at the next one's time makes that one skip. Created paused."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            FixturesWorkflow.run,
            FixturesInput(tournament_id=PREMIER_LEAGUE),
            id=POST_MATCH_SCHEDULE_ID,
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(calendars=[ScheduleCalendarSpec(hour=[ScheduleRange(RUN_AT_HOUR_UTC)])]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
        state=PAUSED,
    )


async def ensure_post_match_schedule(client: Client, task_queue: str) -> str:
    """Create the post-match Schedule, or update it to the current definition.

    Returns "created" or "updated"; running it again changes nothing else.
    """
    return await _ensure(client, POST_MATCH_SCHEDULE_ID, post_match_schedule(task_queue))


def understat_schedule(task_queue: str) -> Schedule:
    """Daily at 07:00 UTC, store the current Premier League season's Understat data and the
    Matches played since the last run. A run still going at the next one's time makes that
    one skip. Created paused."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            UnderstatIngestWorkflow.run,
            UnderstatIngestInput(league=UNDERSTAT_PREMIER_LEAGUE, count=1),
            id=UNDERSTAT_SCHEDULE_ID,
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(
            calendars=[ScheduleCalendarSpec(hour=[ScheduleRange(UNDERSTAT_RUN_AT_HOUR_UTC)])]
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
        state=PAUSED,
    )


async def ensure_understat_schedule(client: Client, task_queue: str) -> str:
    """Create the Understat Schedule, or update it to the current definition.

    Returns "created" or "updated"; running it again changes nothing else.
    """
    return await _ensure(client, UNDERSTAT_SCHEDULE_ID, understat_schedule(task_queue))


async def _ensure(client: Client, schedule_id: str, schedule: Schedule) -> str:
    try:
        await client.create_schedule(schedule_id, schedule)
        return "created"
    except ScheduleAlreadyRunningError:

        def update(_: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(schedule=schedule)

        await client.get_schedule_handle(schedule_id).update(update)
        return "updated"
