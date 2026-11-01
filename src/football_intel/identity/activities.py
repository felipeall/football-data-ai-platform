"""Identity activities: read staged Source Records, write the `identity` schema."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, LiteralString

from psycopg import AsyncConnection
from temporalio import activity
from temporalio.exceptions import ApplicationError

from football_intel.identity.clubs import Team, TransfermarktClub, UnresolvedTeam, plan_clubs
from football_intel.identity.rules import (
    Appearance,
    ReepBridge,
    SourcePlayer,
    reep_proposals,
    transfermarkt_proposals,
    understat_proposals,
)
from football_intel.identity.store import ClubStore, IdentityStore


@dataclass(frozen=True)
class CandidateCount:
    source: str
    method: str
    status: str
    count: int


@dataclass(frozen=True)
class IdentityResolutionResult:
    reep_release: str | None  # the Reep release that decided first; None when skipped
    players_created: int
    candidates_stored: int
    # Every Candidate in the schema after the run, not only this run's.
    candidates: list[CandidateCount]


@dataclass(frozen=True)
class ClubResult:
    reep_release: str
    created: list[str]  # the new Clubs' IDs
    links_added: int
    clubs: int  # every Club after the run
    unresolved: list[UnresolvedTeam]


class IdentityActivities:
    """`reep_release` is the pinned Reep release (ADR 32); None skips the Reep step."""

    def __init__(self, database_url: str, reep_release: str | None = None) -> None:
        self._database_url = database_url
        self._reep_release = reep_release

    @activity.defn
    async def create_clubs(self) -> ClubResult:
        """Create and link the Clubs of every team in the stored Matches of the Competitions
        we cover, in one transaction: through the pinned Reep release's team bridges, named
        after the Transfermarkt club (ADR 34). Needs dbt's staging models. A team that gets
        no Club is returned, never failed on."""
        if self._reep_release is None:
            raise ApplicationError(
                "Set REEP__RELEASE: Clubs are created from the pinned Reep release",
                type="ReepReleaseNotSet",
                non_retryable=True,
            )
        release = self._reep_release
        async with (
            await AsyncConnection.connect(self._database_url) as conn,
            conn.transaction(),
        ):
            await _require_release(conn, release)
            cursor = await conn.execute(_TEAMS)
            teams = [Team(*row) for row in await cursor.fetchall()]
            cursor = await conn.execute(
                """
                SELECT provider, external_id, reep_id FROM reference.reep_team_bridges
                WHERE release = %s AND provider IN ('sofascore', 'understat')
                """,
                (release,),
            )
            reep_teams: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
            for provider, external_id, reep_id in await cursor.fetchall():
                reep_teams[provider, external_id].add(reep_id)
            reep_ids = sorted(
                {r for t in teams for r in reep_teams.get((t.provider, t.external_id), ())}
            )
            cursor = await conn.execute(
                """
                SELECT reep_id, external_id FROM reference.reep_team_bridges
                WHERE release = %s AND provider = 'transfermarkt' AND reep_id = ANY(%s)
                """,
                (release, reep_ids),
            )
            vereins: defaultdict[str, set[str]] = defaultdict(set)
            for reep_id, external_id in await cursor.fetchall():
                vereins[reep_id].add(external_id)
            cursor = await conn.execute(
                """
                SELECT club_id::text, name, display_name, country
                FROM staging.stg_transfermarkt__clubs WHERE club_id::text = ANY(%s)
                """,
                (sorted({v for ids in vereins.values() for v in ids}),),
            )
            transfermarkt = {row[0]: TransfermarktClub(*row) for row in await cursor.fetchall()}
            store = ClubStore(conn)
            plan = plan_clubs(
                teams, reep_teams, vereins, transfermarkt, await store.links(), await store.taken()
            )
            await store.apply(plan, f"reep:{release}")
            cursor = await conn.execute("SELECT count(*) FROM identity.clubs")
            row = await cursor.fetchone()
            assert row is not None
        for team in plan.unresolved:
            activity.logger.warning(
                "No Club for %s team %s (%s): %s",
                team.provider,
                team.external_id,
                team.name,
                team.reason,
            )
        return ClubResult(
            release, [c.club_id for c in plan.clubs], len(plan.links), row[0], plan.unresolved
        )

    @activity.defn
    async def resolve_identities(self) -> IdentityResolutionResult:
        """Link the staged Source Records to Players in one transaction: the pinned Reep
        release's mappings first, then the deterministic rules and fuzzy scoring for what
        Reep did not link (ADR 32). Needs dbt's staging models, the Clubs and `marts.matches`."""
        async with (
            await AsyncConnection.connect(self._database_url) as conn,
            conn.transaction(),
        ):
            sofascore = await _players(
                conn, "SELECT player_id, name, birth_date FROM staging.stg_sofascore__players"
            )
            transfermarkt = await _players(
                conn,
                "SELECT player_id, name, birth_date FROM staging.stg_transfermarkt__players",
            )
            cursor = await conn.execute(
                "SELECT player_id, name FROM staging.stg_understat__players"
            )
            understat_names: dict[int, str] = dict(await cursor.fetchall())
            store = IdentityStore(conn)
            players, created = await store.anchor_sofascore(p.source_record_id for p in sofascore)
            stored = 0
            if self._reep_release is not None:
                held = {
                    "transfermarkt": {p.source_record_id for p in transfermarkt},
                    "understat": set(understat_names),
                }
                for source, records in held.items():
                    bridges = await _reep_bridges(conn, self._reep_release, source)
                    stored += await store.propose(
                        source,
                        reep_proposals(self._reep_release, bridges, records, set(players)),
                        players,
                    )
            stored += await store.propose(
                "transfermarkt", transfermarkt_proposals(transfermarkt, sofascore), players
            )
            stored += await store.propose(
                "understat",
                understat_proposals(
                    await _appearances(conn, "understat"),
                    await _appearances(conn, "sofascore"),
                    understat_names,
                    {p.source_record_id: p.name for p in sofascore},
                ),
                players,
            )
            counts = [CandidateCount(*row) for row in await store.counts()]
        activity.logger.info(
            "Identity Resolution: %d Players created, %d Candidates stored", created, stored
        )
        return IdentityResolutionResult(self._reep_release, created, stored, counts)


async def _players(conn: AsyncConnection[Any], query: LiteralString) -> list[SourcePlayer]:
    cursor = await conn.execute(query)
    return [
        SourcePlayer(player_id, name, birth_date)
        for player_id, name, birth_date in await cursor.fetchall()
    ]


# A source's Appearances in canonical Matches both sources have, for canonical Clubs.
_APPEARANCES: dict[str, LiteralString] = {
    "sofascore": """
        SELECT matches.match_id, clubs.club_id, appearances.player_id, appearances.minutes_played
        FROM staging.stg_sofascore__appearances AS appearances
        JOIN marts.matches AS matches ON matches.sofascore_match_id = appearances.match_id
        JOIN identity.club_links AS clubs
            ON clubs.provider = 'sofascore' AND clubs.external_id = appearances.team_id::text
        WHERE matches.understat_match_id IS NOT NULL
    """,
    "understat": """
        SELECT matches.match_id, clubs.club_id, appearances.player_id, appearances.minutes_played
        FROM staging.stg_understat__appearances AS appearances
        JOIN marts.matches AS matches ON matches.understat_match_id = appearances.match_id
        JOIN identity.club_links AS clubs
            ON clubs.provider = 'understat' AND clubs.external_id = appearances.team_id::text
        WHERE matches.sofascore_match_id IS NOT NULL
    """,
}


async def _appearances(conn: AsyncConnection[Any], source: str) -> list[Appearance]:
    cursor = await conn.execute(_APPEARANCES[source])
    return [
        Appearance(match_id, club_id, player_id, minutes)
        for match_id, club_id, player_id, minutes in await cursor.fetchall()
    ]


# Every team in the stored Matches of the Competitions we cover, with its latest name.
_TEAMS: LiteralString = """
    SELECT DISTINCT ON (provider, external_id) provider, external_id, name
    FROM (
        SELECT 'sofascore' AS provider, team.id::text AS external_id, team.name, m.kickoff_at
        FROM staging.stg_sofascore__matches AS m
        JOIN identity.competition_links AS c
            ON c.provider = 'sofascore' AND c.external_id = m.tournament_id::text
        CROSS JOIN LATERAL (
            VALUES (m.home_team_id, m.home_team_name), (m.away_team_id, m.away_team_name)
        ) AS team (id, name)
        UNION ALL
        SELECT 'understat', team.id::text, team.name, m.kickoff_at
        FROM staging.stg_understat__matches AS m
        JOIN identity.competition_links AS c
            ON c.provider = 'understat' AND c.external_id = m.league
        CROSS JOIN LATERAL (
            VALUES (m.home_team_id, m.home_team_name), (m.away_team_id, m.away_team_name)
        ) AS team (id, name)
    ) AS teams
    ORDER BY provider, external_id, kickoff_at DESC
"""


async def _require_release(conn: AsyncConnection[Any], release: str) -> None:
    """Fails without retries if the release is not loaded: `football-intel load-reep`
    loads it."""
    cursor = await conn.execute(
        "SELECT 1 FROM reference.reep_releases WHERE release = %s", (release,)
    )
    if await cursor.fetchone() is None:
        raise ApplicationError(
            f"Reep release {release} is not loaded; run `football-intel load-reep`",
            type="ReepReleaseNotLoaded",
            non_retryable=True,
        )


async def _reep_bridges(conn: AsyncConnection[Any], release: str, source: str) -> list[ReepBridge]:
    """The release's bridges from `source` players to Sofascore players."""
    await _require_release(conn, release)
    cursor = await conn.execute(
        """
        SELECT other.external_id::bigint, sofascore.external_id::bigint, other.rule_id,
            other.route_scope
        FROM reference.reep_player_bridges AS other
        JOIN reference.reep_player_bridges AS sofascore
            ON sofascore.release = other.release AND sofascore.reep_id = other.reep_id
            AND sofascore.provider = 'sofascore'
        WHERE other.release = %s AND other.provider = %s
            AND other.external_id ~ '^[0-9]+$' AND sofascore.external_id ~ '^[0-9]+$'
        """,
        (release, source),
    )
    return [ReepBridge(*row) for row in await cursor.fetchall()]
