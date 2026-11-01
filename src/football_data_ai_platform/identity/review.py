"""Identity review activities: the LLM judge and human review of Identity Candidates
(ADR 6). The `IdentityReviewWorkflow` runs them; each one is retryable on its own."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, LiteralString

from psycopg import AsyncConnection
from temporalio import activity
from temporalio.exceptions import ApplicationError

from football_data_ai_platform.agent.llm import LLMClient, LLMRefusal
from football_data_ai_platform.identity.judge import (
    LLM_JUDGE,
    SYSTEM,
    JudgeCase,
    RecordFacts,
    Verdict,
    prompt,
)
from football_data_ai_platform.identity.rules import SOFASCORE, TRANSFERMARKT, UNDERSTAT
from football_data_ai_platform.identity.store import IdentityStore, StoredCandidate


@dataclass(frozen=True)
class Judgement:
    """The judge's answer on one Candidate, and the status it left the Candidate in."""

    candidate_id: int
    source: str
    source_record_id: int
    player_id: int
    name: str | None  # the Source Record's name
    player_names: list[str]  # the names of the Player's records
    decision: str  # accept, reject or defer
    confidence: float
    reasoning: str
    status: str  # accepted, rejected or pending (for a person to review)
    not_applied: str | None = None  # why a confident accept still waits for review


@dataclass(frozen=True)
class Review:
    """A reviewer's decision on one Candidate, sent as a signal. `request_id` lets the
    sender find its outcome."""

    request_id: str
    candidate_id: int
    approve: bool
    reviewer: str
    note: str | None = None


@dataclass(frozen=True)
class ReviewOutcome:
    request_id: str
    candidate_id: int
    applied: bool
    status: str | None  # the Candidate's status afterwards; None when there is no Candidate
    message: str | None  # why nothing changed


CaseReader = Callable[[AsyncConnection[Any], StoredCandidate], Awaitable[JudgeCase]]


class IdentityReviewActivities:
    """`judge` builds the `identity_judge` Model Role's client, on first use, so a worker
    without that role configured still runs everything else."""

    def __init__(
        self,
        database_url: str,
        judge: Callable[[], LLMClient],
        read_case: CaseReader | None = None,
    ) -> None:
        self._database_url = database_url
        self._judge_factory = judge
        self._judge: LLMClient | None = None
        self._read_case = read_case or read_case_from_staging

    @activity.defn
    async def judgeable_candidates(self) -> list[int]:
        """The pending Candidates the judge has not answered yet."""
        async with await AsyncConnection.connect(self._database_url, autocommit=True) as conn:
            pending = await IdentityStore(conn).pending()
        return [c.candidate_id for c in pending if c.method != LLM_JUDGE]

    @activity.defn
    async def judge_candidate(self, candidate_id: int) -> Judgement | None:
        """Ask the judge about one Candidate and store its verdict. None when the Candidate
        was decided or judged meanwhile. The model call happens outside the transaction; a
        retry after a failed write asks again."""
        async with await AsyncConnection.connect(self._database_url, autocommit=True) as conn:
            candidate = await _judgeable(conn, candidate_id)
            if candidate is None:
                return None
            case = await self._read_case(conn, candidate)
        judge = self._client()
        try:
            result = await judge.parse(system=SYSTEM, prompt=prompt(case), output=Verdict)
            verdict, model, trace_id = result.output, result.model, result.trace_id
        except LLMRefusal as refusal:
            # No answer is not a decision: a person reviews it.
            verdict = Verdict(decision="defer", confidence=0.0, reasoning=f"no verdict: {refusal}")
            model, trace_id = judge.model, None
        async with (
            await AsyncConnection.connect(self._database_url) as conn,
            conn.transaction(),
        ):
            stored = await IdentityStore(conn).record_verdict(
                candidate_id, verdict, model, trace_id
            )
        if stored is None:
            return None
        judgement = stored.evidence["judge"]
        activity.logger.info(
            "Judged Candidate %d (%s %d → Player %d): %s at %.2f, now %s",
            candidate_id,
            stored.source,
            stored.source_record_id,
            stored.player_id,
            verdict.decision,
            verdict.confidence,
            stored.status,
        )
        return Judgement(
            candidate_id,
            stored.source,
            stored.source_record_id,
            stored.player_id,
            case.record.name,
            [r.name for r in case.player_records if r.name],
            verdict.decision,
            verdict.confidence,
            verdict.reasoning,
            stored.status,
            judgement.get("not_applied"),
        )

    @activity.defn
    async def apply_review(self, review: Review) -> ReviewOutcome:
        """Apply a reviewer's decision. A decision that cannot apply (no such Candidate, or a
        second link) is returned, not raised: retrying would not change it."""
        async with (
            await AsyncConnection.connect(self._database_url) as conn,
            conn.transaction(),
        ):
            stored, message = await IdentityStore(conn).review(
                review.candidate_id, review.approve, review.reviewer, review.note
            )
        activity.logger.info(
            "Review of Candidate %d by %s: %s%s",
            review.candidate_id,
            review.reviewer,
            "approve" if review.approve else "reject",
            f" not applied: {message}" if message else "",
        )
        return ReviewOutcome(
            review.request_id,
            review.candidate_id,
            message is None,
            None if stored is None else stored.status,
            message,
        )

    @activity.defn
    async def awaiting_review(self) -> int:
        """How many Candidates are pending."""
        async with await AsyncConnection.connect(self._database_url, autocommit=True) as conn:
            return len(await IdentityStore(conn).pending())

    def _client(self) -> LLMClient:
        if self._judge is None:
            try:
                self._judge = self._judge_factory()
            except Exception as error:
                raise ApplicationError(
                    f"The identity_judge Model Role is not usable: {error}",
                    type="ModelRoleNotConfigured",
                    non_retryable=True,
                ) from error
        return self._judge


async def _judgeable(conn: AsyncConnection[Any], candidate_id: int) -> StoredCandidate | None:
    async with conn.transaction():
        candidate = await IdentityStore(conn).candidate(candidate_id)
    if candidate is None or candidate.status != "pending" or candidate.method == LLM_JUDGE:
        return None
    return candidate


# Each source's staged players: name and birth date.
_STAGED: dict[str, LiteralString] = {
    SOFASCORE: "SELECT name, birth_date FROM staging.stg_sofascore__players WHERE player_id = %s",
    TRANSFERMARKT: "SELECT name, birth_date FROM staging.stg_transfermarkt__players"
    " WHERE player_id = %s",
    UNDERSTAT: "SELECT name, NULL::date FROM staging.stg_understat__players WHERE player_id = %s",
}
# Each match source's Appearances: Matches and minutes in every stored Match.
_PLAYED: dict[str, LiteralString] = {
    SOFASCORE: "SELECT count(*)::int, coalesce(sum(minutes_played), 0)::int"
    " FROM staging.stg_sofascore__appearances WHERE player_id = %s",
    UNDERSTAT: "SELECT count(*)::int, coalesce(sum(minutes_played), 0)::int"
    " FROM staging.stg_understat__appearances WHERE player_id = %s",
}


async def read_case_from_staging(
    conn: AsyncConnection[Any], candidate: StoredCandidate
) -> JudgeCase:
    """The case the judge sees: the Source Record and every record linked to the Player,
    read from staging now, so a Candidate stored before its evidence grew is judged on the
    same facts as a new one."""
    cursor = await conn.execute(
        """
        SELECT source, source_record_id::bigint FROM identity.identity_candidates
        WHERE player_id = %s AND status = 'accepted' ORDER BY source
        """,
        (candidate.player_id,),
    )
    linked = await cursor.fetchall()
    return JudgeCase(
        candidate.candidate_id,
        candidate.player_id,
        candidate.method,
        candidate.confidence,
        await _facts(conn, candidate.source, candidate.source_record_id),
        [await _facts(conn, source, record) for source, record in linked],
        {k: v for k, v in candidate.evidence.items() if k not in ("judge", "review")},
    )


async def _facts(conn: AsyncConnection[Any], source: str, record: int) -> RecordFacts:
    cursor = await conn.execute(_STAGED[source], (record,))
    row = await cursor.fetchone()
    name, born = row if row is not None else (None, None)
    matches = minutes = None
    if source in _PLAYED:
        cursor = await conn.execute(_PLAYED[source], (record,))
        played = await cursor.fetchone()
        assert played is not None
        matches, minutes = played
    return RecordFacts(source, record, name, born, matches, minutes)
