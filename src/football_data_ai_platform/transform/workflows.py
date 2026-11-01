"""The Transform Run workflow. Deterministic: dbt and Identity Resolution run in activities."""

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from football_data_ai_platform.identity.activities import (
        ClubResult,
        IdentityActivities,
        IdentityResolutionResult,
    )
    from football_data_ai_platform.transform.dbt import DbtActivities, DbtBuildResult

# The fixed workflow ID: a start while a Transform Run is going joins it (ADR 31).
TRANSFORM_WORKFLOW_ID = "transform"

# dbt's selection in each step. Together they build every model and run every test once.
STAGING = ["--select", "staging", "--exclude", "source:identity+"]
CLUBS_AND_MATCHES = ["--select", "clubs", "matches"]  # the exact models, not a `+` selection
THE_REST = ["--exclude", "staging", "clubs", "matches"]


@dataclass(frozen=True)
class StepTime:
    step: str
    seconds: float


@dataclass(frozen=True)
class TransformResult:
    staging: DbtBuildResult
    clubs: ClubResult
    clubs_and_matches: DbtBuildResult
    identity: IdentityResolutionResult
    the_rest: DbtBuildResult
    times: list[StepTime]


@workflow.defn
class TransformWorkflow:
    """One Transform Run (ADR 31, amended by ADR 34), each step an activity:

    1. dbt builds staging;
    2. the Club step creates and links Clubs;
    3. dbt builds `clubs` and `matches`;
    4. Identity Resolution links Source Records to Players, then dbt builds the rest.

    A team or player that links to nothing is in the result, and never fails the run."""

    @workflow.run
    async def run(self) -> TransformResult:
        times: list[StepTime] = []
        started = workflow.now()

        def done(step: str) -> None:
            nonlocal started
            now = workflow.now()
            times.append(StepTime(step, (now - started).total_seconds()))
            started = now

        staging = await _build_dbt(STAGING)
        done("dbt staging")
        clubs = await workflow.execute_activity_method(
            IdentityActivities.create_clubs,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY,
        )
        done("clubs")
        clubs_and_matches = await _build_dbt(CLUBS_AND_MATCHES)
        done("dbt clubs and matches")
        identity = await workflow.execute_activity_method(
            IdentityActivities.resolve_identities,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=_RETRY,
        )
        done("identity resolution")
        the_rest = await _build_dbt(THE_REST)
        done("dbt the rest")
        return TransformResult(staging, clubs, clubs_and_matches, identity, the_rest, times)


# Retries 10 s, then 20 s, later: time for a restarting Postgres to come back.
_RETRY = RetryPolicy(initial_interval=timedelta(seconds=10), maximum_attempts=3)


async def _build_dbt(selection: list[str]) -> DbtBuildResult:
    return await workflow.execute_activity_method(
        DbtActivities.build_dbt,
        selection,
        start_to_close_timeout=timedelta(minutes=30),
        retry_policy=_RETRY,
    )
