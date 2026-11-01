"""The `identity` schema: Players, Identity Candidates and Clubs (ADR 31, ADR 34)."""

from collections.abc import Iterable
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from football_intel.identity.clubs import SLUG, ClubPlan, LinkKey
from football_intel.identity.rules import Method, Proposal, RecordKey


class IdentityStore:
    """Writes Players and Identity Candidates. A Source Record is only ever proposed once:
    a re-run leaves earlier Candidates, accepted, pending or rejected, as they are, except
    that a rejected Reep link reopens its Source Record to the rules."""

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
        """The Source Records that have a Candidate. A record whose only Candidates are
        rejected Reep links is left out unless `reopen_rejected_reep` is False, so the rules
        can link it once a reviewer rejects a wrong Reep link (ADR 32)."""
        cursor = await self._connection.execute(
            """
            SELECT source, source_record_id::bigint FROM identity.identity_candidates
            GROUP BY source, source_record_id
            HAVING %s OR bool_or(NOT (method = %s AND status = 'rejected'))
            """,
            (not reopen_rejected_reep, Method.REEP_BRIDGE),
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
