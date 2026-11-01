"""The `identity` schema: Players, Identity Candidates and Clubs (ADR 31, ADR 34)."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, LiteralString

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from football_data_ai_platform.identity.clubs import SLUG, ClubPlan, LinkKey
from football_data_ai_platform.identity.judge import LLM_JUDGE, Verdict, status_after
from football_data_ai_platform.identity.rules import Method, Proposal, RecordKey


class IdentityStore:
    """Writes Players and Identity Candidates. A Source Record is only ever proposed once:
    a re-run leaves earlier Candidates, accepted, pending or rejected, as they are, except
    that a rejected Reep link reopens its Source Record to the rules, and a rejected creation
    check to creating its Player (`proposed`)."""

    def __init__(self, connection: AsyncConnection[Any]) -> None:
        self._connection = connection

    async def links(self) -> dict[RecordKey, int]:
        """The Player each Source Record links to: its accepted Candidate."""
        cursor = await self._connection.execute(
            """
            SELECT source, source_record_id::bigint, player_id
            FROM identity.identity_candidates WHERE status = 'accepted'
            """
        )
        return {(source, record): player for source, record, player in await cursor.fetchall()}

    async def proposed(self, *, reopen_rejected_reep: bool = True) -> set[RecordKey]:
        """The Source Records that have a Candidate. Some rejections reopen a record, which
        is then left out:

        - a record whose only Candidates are rejected Reep links, unless
          `reopen_rejected_reep` is False, so the rules can link it once the judge or a
          reviewer rejects a wrong Reep link (ADR 32);
        - a record whose only Candidates are rejected creation checks, or those and rejected
          Reep links, so it can create its own Player (ADR 39).

        A judged Candidate keeps the method that proposed it in its evidence.
        """
        reopening = [Method.CREATION_CHECK]
        if reopen_rejected_reep:
            reopening.append(Method.REEP_BRIDGE)
        cursor = await self._connection.execute(
            """
            SELECT source, source_record_id::bigint FROM identity.identity_candidates
            GROUP BY source, source_record_id
            HAVING bool_or(NOT (
                coalesce(evidence->>'proposed_method', method) = ANY(%s) AND status = 'rejected'
            ))
            """,
            (reopening,),
        )
        return {(source, record) for source, record in await cursor.fetchall()}

    async def rejected(self) -> dict[RecordKey, set[int]]:
        """The Players each Source Record's rejected Candidates point at."""
        cursor = await self._connection.execute(
            """
            SELECT source, source_record_id::bigint, player_id
            FROM identity.identity_candidates WHERE status = 'rejected'
            """
        )
        rejected: dict[RecordKey, set[int]] = {}
        for source, record, player in await cursor.fetchall():
            rejected.setdefault((source, record), set()).add(player)
        return rejected

    async def waiting(self) -> set[RecordKey]:
        """The Source Records whose creation check waits for the judge or a person."""
        cursor = await self._connection.execute(
            """
            SELECT source, source_record_id::bigint FROM identity.identity_candidates
            WHERE coalesce(evidence->>'proposed_method', method) = %s AND status = 'pending'
            """,
            (Method.CREATION_CHECK,),
        )
        return {(source, record) for source, record in await cursor.fetchall()}

    async def propose(self, proposals: Iterable[Proposal]) -> tuple[int, int]:
        """Store new Proposals as Candidates: accepted from `ACCEPT_FROM` unless our data
        disagrees, pending otherwise.

        A Proposal for a Source Record that already has a Candidate is skipped, and one that
        would give a Player a second link from the same source waits for the judge instead.
        A Proposal whose Player is a key (`reep:<id>`, `transfermarkt:<id>`) creates that
        Player with its first stored Candidate.

        Returns how many Candidates were stored and how many Players were created.
        """
        proposed = await self.proposed()
        linked = {(source, player) for (source, _), player in (await self.links()).items()}
        created: dict[str, int] = {}
        stored = 0
        for proposal in proposals:
            key = (proposal.source, proposal.source_record_id)
            if key in proposed:
                continue
            if isinstance(proposal.player, int):
                player_id = proposal.player
            elif proposal.player in created:
                player_id = created[proposal.player]
            else:
                cursor = await self._connection.execute(
                    "INSERT INTO identity.players DEFAULT VALUES RETURNING player_id"
                )
                row = await cursor.fetchone()
                assert row is not None
                player_id = created[proposal.player] = row[0]
            accepted = proposal.accepted and (proposal.source, player_id) not in linked
            evidence = proposal.evidence
            if proposal.disagreement is not None:
                evidence = {**evidence, "disagreement": proposal.disagreement}
            inserted = await self._insert(
                proposal.source,
                proposal.source_record_id,
                player_id,
                proposal.method,
                proposal.confidence,
                "accepted" if accepted else "pending",
                evidence,
                proposal.decided_by,
            )
            if not inserted:
                continue  # the same link was proposed before and rejected
            proposed.add(key)
            if accepted:
                linked.add((proposal.source, player_id))
            stored += 1
        return stored, len(created)

    async def candidate(self, candidate_id: int) -> "StoredCandidate | None":
        """One Candidate, locked for the rest of the transaction."""
        cursor = await self._connection.execute(
            f"{_CANDIDATES} WHERE candidate_id = %s FOR UPDATE", (candidate_id,)
        )
        row = await cursor.fetchone()
        return None if row is None else StoredCandidate(*row)

    async def pending(self) -> "list[StoredCandidate]":
        """The pending Candidates, judged or not: what awaits the judge or a reviewer."""
        cursor = await self._connection.execute(
            f"{_CANDIDATES} WHERE status = 'pending' ORDER BY candidate_id"
        )
        return [StoredCandidate(*row) for row in await cursor.fetchall()]

    async def record_verdict(
        self, candidate_id: int, verdict: Verdict, model: str, trace_id: str | None
    ) -> "StoredCandidate | None":
        """Store the judge's verdict on a pending Candidate it has not answered yet: its
        method becomes `llm_judge`, and the proposing method and score move to its evidence.
        It is accepted or rejected only when the judge is sure (`status_after`), and an
        accept that would give a record or a Player a second link stays pending, with the
        reason. Returns the Candidate as stored, or None when it was no longer judgeable."""
        candidate = await self.candidate(candidate_id)
        if candidate is None or candidate.status != "pending" or candidate.method == LLM_JUDGE:
            return None
        status = status_after(verdict)
        conflict = await self._link_conflict(candidate) if status == "accepted" else None
        if conflict is not None:
            status = "pending"
        judgement = {
            "decision": verdict.decision,
            "confidence": verdict.confidence,
            "reasoning": verdict.reasoning,
            "model": model,
            "trace_id": trace_id,
        }
        if conflict is not None:
            judgement["not_applied"] = conflict
        evidence = {
            **candidate.evidence,
            "proposed_method": candidate.method,
            "proposed_confidence": candidate.confidence,
            "judge": judgement,
        }
        return await self._decide(
            candidate_id,
            LLM_JUDGE,
            _link_confidence(verdict, candidate.confidence),
            status,
            evidence,
            f"{LLM_JUDGE}:{model}",
        )

    async def review(
        self, candidate_id: int, approve: bool, reviewer: str, note: str | None = None
    ) -> tuple["StoredCandidate | None", str | None]:
        """A reviewer accepts or rejects a Candidate, whatever its status: a pending one, a
        judge's decision, or an accepted link. Rejecting a Reep link reopens its record to
        the rules (ADR 32). The method stays the one that proposed it.

        Returns the Candidate as stored, and why nothing changed when nothing did."""
        candidate = await self.candidate(candidate_id)
        if candidate is None:
            return None, f"no Candidate {candidate_id}"
        status = "accepted" if approve else "rejected"
        if candidate.status == status:
            return candidate, f"Candidate {candidate_id} is already {status}"
        if approve and (conflict := await self._link_conflict(candidate)) is not None:
            return candidate, conflict
        evidence = {
            **candidate.evidence,
            "review": {
                "decision": "approve" if approve else "reject",
                "reviewer": reviewer,
                "note": note,
                "previous_status": candidate.status,
            },
        }
        stored = await self._decide(
            candidate_id,
            candidate.method,
            candidate.confidence,
            status,
            evidence,
            f"reviewer:{reviewer}",
        )
        return stored, None

    async def _decide(
        self,
        candidate_id: int,
        method: str,
        confidence: float,
        status: str,
        evidence: dict[str, Any],
        decided_by: str,
    ) -> "StoredCandidate":
        cursor = await self._connection.execute(
            """
            UPDATE identity.identity_candidates
            SET method = %(method)s, confidence = %(confidence)s, status = %(status)s,
                evidence = %(evidence)s,
                decided_at = CASE WHEN %(status)s = 'pending' THEN NULL ELSE now() END,
                decided_by = CASE WHEN %(status)s = 'pending' THEN NULL ELSE %(by)s END
            WHERE candidate_id = %(id)s
            RETURNING candidate_id
            """,
            {
                "method": method,
                "confidence": confidence,
                "status": status,
                "evidence": Jsonb(evidence),
                "by": decided_by,
                "id": candidate_id,
            },
        )
        assert await cursor.fetchone() is not None
        stored = await self.candidate(candidate_id)
        assert stored is not None
        return stored

    async def _link_conflict(self, candidate: "StoredCandidate") -> str | None:
        """Why accepting the Candidate would break one link per record or per Player and
        source; None when it would not."""
        cursor = await self._connection.execute(
            """
            SELECT 'record', player_id::text FROM identity.identity_candidates
            WHERE source = %(source)s AND source_record_id = %(record)s
                AND status = 'accepted' AND candidate_id <> %(id)s
            UNION ALL
            SELECT 'player', source_record_id FROM identity.identity_candidates
            WHERE player_id = %(player)s AND source = %(source)s
                AND status = 'accepted' AND candidate_id <> %(id)s
            """,
            {
                "source": candidate.source,
                "record": str(candidate.source_record_id),
                "player": candidate.player_id,
                "id": candidate.candidate_id,
            },
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        kind, other = row
        if kind == "record":
            return (
                f"{candidate.source} {candidate.source_record_id} already links to Player"
                f" {other}; reject that link first"
            )
        return (
            f"Player {candidate.player_id} already links to {candidate.source} {other};"
            " reject that link first"
        )

    async def counts(self) -> list[tuple[str, str, str, int]]:
        """Candidates by source, method and status."""
        cursor = await self._connection.execute(
            """
            SELECT source, method, status, count(*) FROM identity.identity_candidates
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """
        )
        return [
            (source, method, status, count)
            for source, method, status, count in await cursor.fetchall()
        ]

    async def _insert(
        self,
        source: str,
        source_record_id: int,
        player_id: int,
        method: str,
        confidence: float,
        status: str,
        evidence: dict[str, Any],
        decided_by: str | None = None,
    ) -> bool:
        """Whether the Candidate was stored: a pair already proposed, and rejected, is not."""
        cursor = await self._connection.execute(
            """
            INSERT INTO identity.identity_candidates
                (source, source_record_id, player_id, method, confidence, status, evidence,
                 decided_at, decided_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s = 'pending' THEN NULL ELSE now() END,
                    CASE WHEN %s = 'pending' THEN NULL ELSE %s END)
            ON CONFLICT (source, source_record_id, player_id) DO NOTHING
            """,
            (
                source,
                str(source_record_id),
                player_id,
                method,
                confidence,
                status,
                Jsonb(evidence),
                status,
                status,
                decided_by or f"rule:{method}",
            ),
        )
        return cursor.rowcount == 1


@dataclass(frozen=True)
class StoredCandidate:
    candidate_id: int
    source: str
    source_record_id: int
    player_id: int
    method: str
    confidence: float
    status: str
    evidence: dict[str, Any]
    decided_by: str | None


_CANDIDATES: LiteralString = """
    SELECT candidate_id, source, source_record_id::bigint, player_id, method,
        confidence::float, status, evidence, decided_by
    FROM identity.identity_candidates
"""


def _link_confidence(verdict: Verdict, proposed: float) -> float:
    """How likely the link is right, after the verdict: the judge's confidence in an
    accept, its complement for a reject; a deferral keeps the proposal's score."""
    if verdict.decision == "accept":
        return verdict.confidence
    if verdict.decision == "reject":
        return round(1 - verdict.confidence, 3)
    return proposed


class ClubTaken(Exception):
    """A rename's new slug is another Club's ID or alias, or not a slug."""


class ClubStore:
    """Writes Clubs and their links and aliases (ADR 34). Club IDs change only by rename."""

    def __init__(self, connection: AsyncConnection[Any]) -> None:
        self._connection = connection

    async def links(self) -> dict[LinkKey, str]:
        cursor = await self._connection.execute(
            "SELECT provider, external_id, club_id FROM identity.club_links"
        )
        return {(provider, external): club for provider, external, club in await cursor.fetchall()}

    async def taken(self) -> set[str]:
        """Every Club ID and former Club ID."""
        cursor = await self._connection.execute(
            "SELECT club_id FROM identity.clubs UNION SELECT alias FROM identity.club_aliases"
        )
        return {row[0] for row in await cursor.fetchall()}

    async def apply(self, plan: ClubPlan, method: str) -> None:
        for club in plan.clubs:
            await self._connection.execute(
                "INSERT INTO identity.clubs (club_id, name, display_name, country)"
                " VALUES (%s, %s, %s, %s)",
                (club.club_id, club.name, club.display_name, club.country),
            )
        for (provider, external_id), club_id in sorted(plan.links.items()):
            await self._connection.execute(
                "INSERT INTO identity.club_links (provider, external_id, club_id, method)"
                " VALUES (%s, %s, %s, %s)",
                (provider, external_id, club_id, method),
            )

    async def rename(self, club_id: str, new_id: str) -> None:
        """Give a Club a new slug. The old one becomes an alias, so the Match IDs built from
        it keep resolving. Renaming back to a former slug of the same Club reclaims it."""
        if not SLUG.fullmatch(new_id):
            raise ClubTaken(f"{new_id!r} is not a slug: lower-case words joined by '-'")
        cursor = await self._connection.execute(
            """
            SELECT 'club' FROM identity.clubs WHERE club_id = %(new)s
            UNION ALL
            SELECT 'alias' FROM identity.club_aliases WHERE alias = %(new)s AND club_id <> %(old)s
            """,
            {"new": new_id, "old": club_id},
        )
        if await cursor.fetchone() is not None:
            raise ClubTaken(f"{new_id} is already a Club ID or another Club's former ID")
        cursor = await self._connection.execute(
            "SELECT 1 FROM identity.clubs WHERE club_id = %s", (club_id,)
        )
        if await cursor.fetchone() is None:
            raise ClubTaken(f"no Club {club_id}")
        await self._connection.execute(
            "DELETE FROM identity.club_aliases WHERE alias = %s", (new_id,)
        )
        await self._connection.execute(
            "UPDATE identity.clubs SET club_id = %s WHERE club_id = %s", (new_id, club_id)
        )
        await self._connection.execute(
            "INSERT INTO identity.club_aliases (alias, club_id) VALUES (%s, %s)",
            (club_id, new_id),
        )
