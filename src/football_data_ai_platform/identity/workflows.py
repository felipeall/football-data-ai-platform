"""The identity review workflow: the LLM judge, then human review by signal (ADR 6).

Deterministic: the database and the model are reached in activities only.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from football_data_ai_platform.identity.review import (
        IdentityReviewActivities,
        Judgement,
        Review,
        ReviewOutcome,
    )

# The fixed workflow ID: `fdap judge` and `fdap review` signal the one that is running, or
# start it.
IDENTITY_REVIEW_WORKFLOW_ID = "identity-review"


@dataclass(frozen=True)
class JudgeRequest:
    """Judge every pending Candidate the judge has not answered yet."""

    request_id: str


@dataclass(frozen=True)
class JudgePass:
    request_id: str
    judgements: list[Judgement]
    # Why the pass stopped early: Candidates after the failing one stay unjudged.
    error: str | None = None


@dataclass(frozen=True)
class ReviewStatus:
    """What the workflow has done so far, for the CLI to read its own request's outcome."""

    judge_passes: list[JudgePass] = field(default_factory=list[JudgePass])
    reviews: list[ReviewOutcome] = field(default_factory=list[ReviewOutcome])


@workflow.defn
class IdentityReviewWorkflow:
    """Takes `judge` and `review` signals, in the order they arrive:

    - `judge`: each pending Candidate the judge has not answered is judged in its own
      activity, retried on failure. A confident verdict decides it; the rest wait for review.
    - `review`: a reviewer's approve or reject is applied to one Candidate.

    It keeps waiting for reviews while any Candidate is pending, and ends once none is and
    no signal waits. The next signal starts it again."""

    def __init__(self) -> None:
        self._requests: list[JudgeRequest | Review] = []
        self._status = ReviewStatus()

    @workflow.signal
    def judge(self, request: JudgeRequest) -> None:
        self._requests.append(request)

    @workflow.signal
    def review(self, review: Review) -> None:
        self._requests.append(review)

    @workflow.query
    def status(self) -> ReviewStatus:
        return self._status

    @workflow.run
    async def run(self) -> ReviewStatus:
        while True:
            await workflow.wait_condition(lambda: bool(self._requests))
            while self._requests:
                request = self._requests.pop(0)
                if isinstance(request, JudgeRequest):
                    self._status.judge_passes.append(await self._judge(request))
                else:
                    self._status.reviews.append(
                        await workflow.execute_activity_method(
                            IdentityReviewActivities.apply_review,
                            request,
                            start_to_close_timeout=timedelta(minutes=1),
                            retry_policy=_RETRY,
                        )
                    )
            pending = await workflow.execute_activity_method(
                IdentityReviewActivities.awaiting_review,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=_RETRY,
            )
            if pending == 0 and not self._requests:
                return self._status

    async def _judge(self, request: JudgeRequest) -> JudgePass:
        candidates = await workflow.execute_activity_method(
            IdentityReviewActivities.judgeable_candidates,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=_RETRY,
        )
        judgements: list[Judgement] = []
        for candidate_id in candidates:
            try:
                judgement = await workflow.execute_activity_method(
                    IdentityReviewActivities.judge_candidate,
                    candidate_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=_JUDGE_RETRY,
                )
            except ActivityError as error:
                # The workflow keeps serving reviews; a later `judge` signal resumes.
                cause = error.cause or error
                return JudgePass(
                    request.request_id, judgements, f"Candidate {candidate_id}: {cause}"
                )
            if judgement is not None:
                judgements.append(judgement)
        return JudgePass(request.request_id, judgements)


_RETRY = RetryPolicy(maximum_attempts=3)
# A model call fails on rate limits and overloads, which pass with time.
_JUDGE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
    non_retryable_error_types=["ModelRoleNotConfigured"],
)
