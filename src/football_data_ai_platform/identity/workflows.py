"""Identity workflows. Deterministic: the work of each is one activity."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from football_data_ai_platform.identity.activities import (
        ClubResult,
        IdentityActivities,
        IdentityResolutionResult,
    )


@workflow.defn
class IdentityResolutionWorkflow:
    """Link the staged Source Records to Players (ADR 6). Part of the Transform Run (ADR 31),
    between building staging and building the marts that read the `identity` schema."""

    @workflow.run
    async def run(self) -> IdentityResolutionResult:
        return await workflow.execute_activity_method(
            IdentityActivities.resolve_identities,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


@workflow.defn
class ClubWorkflow:
    """Create and link the Clubs of the stored Matches' teams (ADR 34). Step 2 of the
    Transform Run, between building staging and building the Club and Match marts."""

    @workflow.run
    async def run(self) -> ClubResult:
        return await workflow.execute_activity_method(
            IdentityActivities.create_clubs,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
