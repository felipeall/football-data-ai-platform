"""The `identity` schema: Players, Identity Candidates and Clubs (ADR 31, ADR 34)."""

from collections.abc import Iterable
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from football_intel.identity.clubs import SLUG, ClubPlan, LinkKey
from football_intel.identity.rules import Method, Proposal

SOFASCORE = "sofascore"


class IdentityStore:
    """Writes Players and Identity Candidates. A Source Record is only ever proposed once:
    a re-run leaves earlier Candidates, accepted, pending or rejected, as they are, except
    that a rejected Reep link reopens its Source Record."""

    def __init__(self, connection: AsyncConnection[Any]) -> None:
        self._connection = connection

    async def anchor_sofascore(
        self, sofascore_player_ids: Iterable[int]
    ) -> tuple[dict[int, int], int]:
        """The Player of every Sofascore player, created for those that have none yet.

        Returns the Player IDs by Sofascore player ID, and how many Players were created.
        """
        cursor = await self._connection.execute(
            """
            SELECT source_record_id::bigint, player_id FROM identity.identity_candidates
            WHERE source = %s AND status = 'accepted'
            """,
            (SOFASCORE,),
        )
        players: dict[int, int] = dict(await cursor.fetchall())
        created = 0
        for sofascore_id in sorted(set(sofascore_player_ids) - players.keys()):
            cursor = await self._connection.execute(
                "INSERT INTO identity.players DEFAULT VALUES RETURNING player_id"
            )
            row = await cursor.fetchone()
            assert row is not None
            players[sofascore_id] = row[0]
            await self._insert(
                SOFASCORE, sofascore_id, row[0], Method.SOFASCORE_ANCHOR, 1.0, "accepted", {}
            )
            created += 1
        return players, created

    async def propose(
        self, source: str, proposals: Iterable[Proposal], players: dict[int, int]
    ) -> int:
        """Store new Proposals as Candidates: accepted from `ACCEPT_FROM`, pending below.

        A Proposal for a Source Record that already has a Candidate is skipped, and one that
        would give a Player a second link from the same source waits for the judge instead.
        Returns how many Candidates were stored.
        """
        # A Source Record whose only Candidates are rejected Reep links is open again, so the
        # rules can link it once a reviewer rejects a wrong Reep link (ADR 32).
        cursor = await self._connection.execute(
            """
            SELECT source_record_id FROM identity.identity_candidates WHERE source = %s
            GROUP BY source_record_id
            HAVING bool_or(NOT (method = %s AND status = 'rejected'))
            """,
            (source, Method.REEP_BRIDGE),
        )
        proposed = {row[0] for row in await cursor.fetchall()}
        cursor = await self._connection.execute(
            """
            SELECT player_id FROM identity.identity_candidates
            WHERE source = %s AND status = 'accepted'
            """,
            (source,),
        )
        linked = {row[0] for row in await cursor.fetchall()}
        stored = 0
        for proposal in proposals:
            if str(proposal.source_record_id) in proposed:
                continue
            player_id = players[proposal.sofascore_player_id]
            accepted = proposal.accepted and player_id not in linked
            inserted = await self._insert(
                source,
                proposal.source_record_id,
                player_id,
                proposal.method,
                proposal.confidence,
                "accepted" if accepted else "pending",
                proposal.evidence,
                proposal.decided_by,
            )
            if not inserted:
                continue  # the same link was proposed before and rejected
            if accepted:
                linked.add(player_id)
            stored += 1
        return stored

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
