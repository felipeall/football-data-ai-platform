"""The LLM judge and human review of Identity Candidates (ADR 6), with a scripted LLMClient."""

import uuid
from datetime import date
from typing import Any

import pytest
from psycopg import AsyncConnection
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from football_data_ai_platform.agent.llm import LLMRefusal
from football_data_ai_platform.agent.scripted import ScriptedLLMClient
from football_data_ai_platform.identity.judge import (
    SYSTEM,
    JudgeCase,
    RecordFacts,
    Verdict,
    prompt,
    status_after,
)
from football_data_ai_platform.identity.review import (
    IdentityReviewActivities,
    Judgement,
    Review,
)
from football_data_ai_platform.identity.rules import Method, Proposal
from football_data_ai_platform.identity.store import IdentityStore, StoredCandidate
from football_data_ai_platform.identity.workflows import (
    IDENTITY_REVIEW_WORKFLOW_ID,
    IdentityReviewWorkflow,
    JudgeRequest,
    ReviewStatus,
)
from football_data_ai_platform.ingestion.cli import signal_identity_review, wait_for_request

ACCEPT = Verdict(decision="accept", confidence=0.95, reasoning="Dan is short for Daniel.")
REJECT = Verdict(decision="reject", confidence=0.95, reasoning="Born three years apart.")
DEFER = Verdict(decision="defer", confidence=0.5, reasoning="Nothing settles it.")
UNSURE_ACCEPT = Verdict(decision="accept", confidence=0.7, reasoning="Probably.")


async def _case(conn: AsyncConnection[Any], candidate: StoredCandidate) -> JudgeCase:
    """The case without staging: the tests' database has no dbt models."""
    return JudgeCase(
        candidate.candidate_id,
        candidate.player_id,
        candidate.method,
        candidate.confidence,
        RecordFacts(candidate.source, candidate.source_record_id, "Daniel Bentley"),
        [RecordFacts("transfermarkt", 10, "Dan Bentley", date(1993, 7, 13))],
        candidate.evidence,
    )


@pytest.fixture
async def identity(connection: AsyncConnection[Any]) -> IdentityStore:
    await connection.execute(
        "TRUNCATE identity.identity_candidates, identity.players RESTART IDENTITY"
    )
    store = IdentityStore(connection)
    # Player 1 (Transfermarkt 10) and a fuzzy Sofascore Candidate for it, pending: Candidate 2.
    await store.propose(
        [
            Proposal("transfermarkt", 10, "transfermarkt:10", Method.TRANSFERMARKT_PLAYER, 1.0),
            Proposal(
                "sofascore",
                101367,
                1,
                Method.FUZZY,
                0.88,
                {"name": "Daniel Bentley", "candidate": "Dan Bentley"},
            ),
        ]
    )
    return store


def _activities(database_url: str, client: ScriptedLLMClient) -> IdentityReviewActivities:
    return IdentityReviewActivities(database_url, lambda: client, read_case=_case)


async def _judge(
    database_url: str, client: ScriptedLLMClient, candidate_id: int = 2
) -> Judgement | None:
    activities = _activities(database_url, client)
    return await ActivityEnvironment().run(activities.judge_candidate, candidate_id)


async def _stored(identity: IdentityStore, candidate_id: int = 2) -> StoredCandidate:
    async with identity._connection.transaction():  # pyright: ignore[reportPrivateUsage]
        candidate = await identity.candidate(candidate_id)
    assert candidate is not None
    return candidate


def test_only_a_confident_accept_or_reject_decides() -> None:
    assert status_after(ACCEPT) == "accepted"
    assert status_after(REJECT) == "rejected"
    assert status_after(DEFER) == "pending"
    assert status_after(UNSURE_ACCEPT) == "pending"
    assert status_after(Verdict(decision="defer", confidence=1.0, reasoning="")) == "pending"


def test_the_judge_reads_the_record_the_player_s_records_and_the_evidence() -> None:
    case = JudgeCase(
        7,
        1,
        Method.FUZZY,
        0.88,
        RecordFacts("understat", 13200, "Fernando López", matches=9, minutes=410),
        [RecordFacts("sofascore", 1526628, "Fer López", date(2004, 1, 24), 9, 405)],
        {"co_appearance": 1.0, "minutes_difference": 3.9},
    )

    text = prompt(case)

    for fact in ('"Fernando López"', '"Fer López"', '"2004-01-24"', '"co_appearance": 1.0'):
        assert fact in text
    assert "birth_date" not in text.split('"player_records"')[0]  # Understat has none


async def test_a_confident_accept_links_the_record(
    identity: IdentityStore, database_url: str
) -> None:
    client = ScriptedLLMClient([ACCEPT])

    judgement = await _judge(database_url, client)

    assert judgement is not None
    assert (judgement.decision, judgement.status, judgement.name) == (
        "accept",
        "accepted",
        "Daniel Bentley",
    )
    stored = await _stored(identity)
    assert (stored.method, stored.status, stored.confidence, stored.decided_by) == (
        "llm_judge",
        "accepted",
        0.95,
        "llm_judge:scripted",
    )
    assert stored.evidence["proposed_method"] == "fuzzy"
    assert stored.evidence["proposed_confidence"] == 0.88
    assert stored.evidence["judge"]["reasoning"] == ACCEPT.reasoning
    assert (await identity.links())[("sofascore", 101367)] == 1
    assert client.calls[0].system == SYSTEM
    assert '"Dan Bentley"' in client.calls[0].prompt


async def test_a_confident_reject_rejects_the_candidate_for_good(
    identity: IdentityStore, database_url: str
) -> None:
    judgement = await _judge(database_url, ScriptedLLMClient([REJECT]))

    assert judgement is not None
    assert judgement.status == "rejected"
    stored = await _stored(identity)
    assert (stored.status, stored.confidence) == ("rejected", 0.05)
    # The record keeps its rejected Candidate, so the rules do not propose it again.
    assert ("sofascore", 101367) in await identity.proposed()


@pytest.mark.parametrize("verdict", [DEFER, UNSURE_ACCEPT])
async def test_a_deferral_or_an_unsure_verdict_waits_for_review(
    identity: IdentityStore, database_url: str, verdict: Verdict
) -> None:
    client = ScriptedLLMClient([verdict])

    judgement = await _judge(database_url, client)

    assert judgement is not None
    assert judgement.status == "pending"
    stored = await _stored(identity)
    assert (stored.method, stored.status, stored.decided_by) == ("llm_judge", "pending", None)
    assert stored.evidence["judge"]["decision"] == verdict.decision
    # Judged once: it is left to a reviewer, not asked again.
    activities = _activities(database_url, client)
    assert await ActivityEnvironment().run(activities.judgeable_candidates) == []
    assert await _judge(database_url, client) is None
    assert len(client.calls) == 1


async def test_a_refusal_waits_for_review(identity: IdentityStore, database_url: str) -> None:
    judgement = await _judge(database_url, ScriptedLLMClient([LLMRefusal("declined")]))

    assert judgement is not None
    assert (judgement.decision, judgement.status) == ("defer", "pending")
    assert "declined" in judgement.reasoning


async def test_an_accept_that_would_give_the_player_a_second_link_waits_for_review(
    identity: IdentityStore, database_url: str
) -> None:
    await identity.propose([Proposal("sofascore", 5, 1, Method.NAME_BIRTH_DATE, 1.0)])
    # The rule's link came after the fuzzy Candidate, as a later run's could.
    await identity._connection.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE identity.identity_candidates SET status = 'accepted' WHERE source_record_id = '5'"
    )

    judgement = await _judge(database_url, ScriptedLLMClient([ACCEPT]))

    assert judgement is not None
    assert judgement.status == "pending"
    assert judgement.not_applied is not None
    assert "Player 1 already links to sofascore 5" in judgement.not_applied


async def test_a_judged_reep_link_rejected_reopens_its_record(
    identity: IdentityStore, database_url: str
) -> None:
    await identity.propose(
        [
            Proposal(
                "understat",
                802,
                1,
                Method.REEP_BRIDGE,
                1.0,
                disagreement="never played in the same Match",
            )
        ]
    )
    assert ("understat", 802) in await identity.proposed()

    judgement = await _judge(database_url, ScriptedLLMClient([REJECT]), candidate_id=3)

    assert judgement is not None
    assert judgement.status == "rejected"
    assert ("understat", 802) not in await identity.proposed()


async def test_a_judged_creation_check_waits_until_decided_then_reopens_on_reject(
    identity: IdentityStore, database_url: str
) -> None:
    # Sofascore 5 may be Player 1: it creates no Player while its creation check waits.
    await identity.propose([Proposal("sofascore", 5, 1, Method.CREATION_CHECK, 0.8)])

    judgement = await _judge(database_url, ScriptedLLMClient([UNSURE_ACCEPT]), candidate_id=3)

    assert judgement is not None
    assert judgement.status == "pending"
    assert ("sofascore", 5) in await identity.waiting()

    stored, message = await identity.review(3, approve=False, reviewer="felipe", note=None)

    assert message is None
    assert stored is not None
    assert (stored.method, stored.status) == ("llm_judge", "rejected")
    assert ("sofascore", 5) not in await identity.waiting()
    assert ("sofascore", 5) not in await identity.proposed()


async def test_a_reviewer_rejects_an_accepted_reep_link_and_the_record_reopens(
    identity: IdentityStore,
) -> None:
    await identity.propose([Proposal("understat", 802, 1, Method.REEP_BRIDGE, 1.0)])

    stored, message = await identity.review(3, approve=False, reviewer="felipe", note="André")

    assert message is None
    assert stored is not None
    assert (stored.method, stored.status, stored.decided_by) == (
        "reep_bridge",
        "rejected",
        "reviewer:felipe",
    )
    assert stored.evidence["review"]["previous_status"] == "accepted"
    assert ("understat", 802) not in await identity.proposed()


async def test_a_review_that_cannot_apply_changes_nothing(identity: IdentityStore) -> None:
    assert await identity.review(99, approve=True, reviewer="felipe") == (None, "no Candidate 99")
    stored, message = await identity.review(1, approve=True, reviewer="felipe")
    assert stored is not None
    assert message == "Candidate 1 is already accepted"


def _worker(
    env: WorkflowEnvironment, task_queue: str, activities: IdentityReviewActivities
) -> Worker:
    return Worker(
        env.client,
        task_queue=task_queue,
        workflows=[IdentityReviewWorkflow],
        activities=[
            activities.judgeable_candidates,
            activities.judge_candidate,
            activities.apply_review,
            activities.awaiting_review,
        ],
    )


async def test_the_judge_then_a_review_signal_settle_the_band(
    identity: IdentityStore, database_url: str, temporal: WorkflowEnvironment
) -> None:
    client = ScriptedLLMClient([DEFER])
    task_queue = f"test-{uuid.uuid4()}"
    async with _worker(temporal, task_queue, _activities(database_url, client)):
        request = JudgeRequest(str(uuid.uuid4()))
        handle = await signal_identity_review(temporal.client, task_queue, request)
        judged = await wait_for_request(handle, lambda s: next(iter(s.judge_passes), None))
        assert [(j.candidate_id, j.status) for j in judged.judgements] == [(2, "pending")]
        # Candidate 2 is still pending, so the workflow waits for a reviewer.
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING

        bad = Review(str(uuid.uuid4()), 99, approve=True, reviewer="felipe")
        good = Review(str(uuid.uuid4()), 2, approve=True, reviewer="felipe", note="Dan")
        for review in (bad, good):
            again = await signal_identity_review(temporal.client, task_queue, review)
            assert again.run_id == handle.run_id
        status: ReviewStatus = await handle.result()

    assert handle.id == IDENTITY_REVIEW_WORKFLOW_ID
    assert [(r.candidate_id, r.applied, r.status) for r in status.reviews] == [
        (99, False, None),
        (2, True, "accepted"),
    ]
    stored = await _stored(identity)
    assert (stored.method, stored.status, stored.decided_by) == (
        "llm_judge",
        "accepted",
        "reviewer:felipe",
    )
    assert stored.evidence["review"]["note"] == "Dan"


async def test_a_judge_failure_keeps_the_workflow_serving_reviews(
    identity: IdentityStore, database_url: str, temporal: WorkflowEnvironment
) -> None:
    def unconfigured() -> ScriptedLLMClient:
        raise RuntimeError("Set MODELS__IDENTITY_JUDGE__MODEL")

    activities = IdentityReviewActivities(database_url, unconfigured, read_case=_case)
    task_queue = f"test-{uuid.uuid4()}"
    async with _worker(temporal, task_queue, activities):
        handle = await signal_identity_review(
            temporal.client, task_queue, JudgeRequest(str(uuid.uuid4()))
        )
        judged = await wait_for_request(handle, lambda s: next(iter(s.judge_passes), None))
        assert judged.judgements == []
        assert judged.error is not None
        assert "MODELS__IDENTITY_JUDGE__MODEL" in judged.error

        await signal_identity_review(
            temporal.client, task_queue, Review(str(uuid.uuid4()), 2, False, "felipe")
        )
        status = await handle.result()

    assert [(r.candidate_id, r.status) for r in status.reviews] == [(2, "rejected")]
