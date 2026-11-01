"""Identity activities: read staged Source Records, write the `identity` schema."""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, LiteralString

from psycopg import AsyncConnection
from temporalio import activity
from temporalio.exceptions import ApplicationError

from football_data_ai_platform.identity.clubs import (
    Team,
    TransfermarktClub,
    UnresolvedTeam,
    plan_clubs,
)
from football_data_ai_platform.identity.rules import (
    SOFASCORE,
    TRANSFERMARKT,
    UNDERSTAT,
    Appearance,
    Proposal,
    RecordKey,
    ReepBridge,
    ReepConflict,
    SourcePlayer,
    reep_conflicts,
    reep_proposals,
    sofascore_proposals,
    transfermarkt_players,
    understat_proposals,
)
from football_data_ai_platform.identity.store import ClubStore, IdentityStore


@dataclass(frozen=True)
class CandidateCount:
    source: str
    method: str
    status: str
    count: int


@dataclass(frozen=True)
class UnlinkedRecord:
    """A Sofascore or Understat record that links to no Player."""

    source: str
    source_record_id: int
    name: str
    minutes: int  # in the covered Season's stored Matches
    pending: bool  # whether a Candidate waits for the judge


@dataclass(frozen=True)
class IdentityResolutionResult:
    reep_release: str | None  # the Reep release that decided first; None when skipped
    covered_season: int  # by its start year (ADR 35)
    players_created: int
    candidates_stored: int
    # Every Candidate in the schema after the run, not only this run's.
    candidates: list[CandidateCount]
    # Only records with an Appearance in the covered Season (ADR 35): records that played
    # only in other stored Seasons, or never played, are out of scope.
    unlinked: list[UnlinkedRecord]
    reep_conflicts: list[ReepConflict]


@dataclass(frozen=True)
class ClubResult:
    reep_release: str
    created: list[str]  # the new Clubs' IDs
    links_added: int
    clubs: int  # every Club after the run
    unresolved: list[UnresolvedTeam]


class IdentityActivities:
    """`reep_release` is the pinned Reep release (ADR 32); None skips the Reep step.
    `covered_season` is the Season the PoC covers (ADR 35), by its start year: Identity
    Resolution lists and counts only the unlinked records that played in it."""

    def __init__(
        self, database_url: str, reep_release: str | None = None, *, covered_season: int
    ) -> None:
        self._database_url = database_url
        self._reep_release = reep_release
        self._covered_season = covered_season

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
        """Create Players and link the staged Source Records to them, in one transaction
        (`resolve`). Needs dbt's staging models, the Clubs and `marts.matches`."""
        async with (
            await AsyncConnection.connect(self._database_url) as conn,
            conn.transaction(),
        ):
            records = SourceRecords(
                await _players(
                    conn, "SELECT player_id, name, birth_date FROM staging.stg_sofascore__players"
                ),
                await _players(
                    conn,
                    "SELECT player_id, name, birth_date FROM staging.stg_transfermarkt__players",
                ),
                await _players(
                    conn, "SELECT player_id, name, NULL::date FROM staging.stg_understat__players"
                ),
                await _appearances(conn, SOFASCORE),
                await _appearances(conn, UNDERSTAT),
            )
            bridges = (
                None
                if self._reep_release is None
                else await _reep_bridges(conn, self._reep_release)
            )
            store = IdentityStore(conn)
            resolution = await resolve(store, records, self._reep_release, bridges)
            minutes = await _minutes(conn, self._covered_season)
            counts = [CandidateCount(*row) for row in await store.counts()]
        unlinked = [
            UnlinkedRecord(
                source,
                player.source_record_id,
                player.name,
                minutes[source, player.source_record_id],
                pending,
            )
            for source, player, pending in resolution.unlinked
            if (source, player.source_record_id) in minutes
        ]
        for conflict in resolution.reep_conflicts:
            activity.logger.warning(
                "Reep person %s links to several Players (%s) through %s; Reep linked none of"
                " its records",
                conflict.reep_id,
                ", ".join(map(str, conflict.player_ids)),
                ", ".join(f"{source} {record}" for source, record in conflict.records),
            )
        activity.logger.info(
            "Identity Resolution: %d Players created, %d Candidates stored, %d records that"
            " played in Season %d unlinked",
            resolution.players_created,
            resolution.candidates_stored,
            len(unlinked),
            self._covered_season,
        )
        return IdentityResolutionResult(
            self._reep_release,
            self._covered_season,
            resolution.players_created,
            resolution.candidates_stored,
            counts,
            unlinked,
            resolution.reep_conflicts,
        )


@dataclass(frozen=True)
class SourceRecords:
    """The staged players of each source, and their Appearances in canonical Matches both
    match sources have, for canonical Clubs."""

    sofascore: list[SourcePlayer]
    transfermarkt: list[SourcePlayer]
    understat: list[SourcePlayer]
    sofascore_appearances: list[Appearance]
    understat_appearances: list[Appearance]

    def held(self) -> dict[str, list[SourcePlayer]]:
        return {
            SOFASCORE: self.sofascore,
            TRANSFERMARKT: self.transfermarkt,
            UNDERSTAT: self.understat,
        }


@dataclass
class Resolution:
    players_created: int = 0
    candidates_stored: int = 0
    # The Sofascore and Understat records with no Player, and whether a Candidate waits.
    unlinked: list[tuple[str, SourcePlayer, bool]] = field(
        default_factory=list[tuple[str, SourcePlayer, bool]]
    )
    # Reep persons whose records already link to several Players; Reep linked none of them.
    reep_conflicts: list[ReepConflict] = field(default_factory=list[ReepConflict])


async def resolve(
    store: IdentityStore,
    records: SourceRecords,
    reep_release: str | None,
    bridges: list[ReepBridge] | None,
) -> Resolution:
    """Create Players and link Source Records to them (ADR 34), in order:

    1. the Reep release's persons (`bridges`), skipped when there is no release;
    2. a Player for each Transfermarkt record Reep did not link;
    3. Sofascore records, by birth date and name against the Players' Transfermarkt records;
    4. Understat records, by co-appearance with the Players' Sofascore Appearances.

    Each step sees the links of the steps before it. Sofascore and Understat create no
    Player: what links to none is listed as unlinked.
    """
    resolution = Resolution()
    held = records.held()

    async def propose(proposals: list[Proposal]) -> dict[str, dict[int, int]]:
        stored, created = await store.propose(proposals)
        resolution.candidates_stored += stored
        resolution.players_created += created
        return _by_source(await store.links())

    if reep_release is not None and bridges is not None:
        ours = {source: {p.source_record_id for p in ps} for source, ps in held.items()}
        resolution.reep_conflicts = reep_conflicts(bridges, ours, await store.links())
        await propose(
            reep_proposals(
                reep_release,
                bridges,
                ours,
                await store.proposed(reopen_rejected_reep=False),
                await store.links(),
                {
                    (source, p.source_record_id): p.birth_date
                    for source, ps in held.items()
                    for p in ps
                },
                records.sofascore_appearances,
                records.understat_appearances,
            )
        )
    links = await propose(transfermarkt_players(records.transfermarkt))

    by_id = {source: {p.source_record_id: p for p in ps} for source, ps in held.items()}
    with_sofascore = set(links[SOFASCORE].values())
    links = await propose(
        sofascore_proposals(
            records.sofascore,
            {
                player: by_id[TRANSFERMARKT][record]
                for record, player in links[TRANSFERMARKT].items()
                if player not in with_sofascore and record in by_id[TRANSFERMARKT]
            },
        )
    )

    links = await propose(
        understat_proposals(
            records.understat_appearances,
            records.sofascore_appearances,
            {p.source_record_id: p.name for p in records.understat},
            {p.source_record_id: p.name for p in records.sofascore},
            links[SOFASCORE],
        )
    )

    proposed = await store.proposed()
    resolution.unlinked = [
        (source, player, (source, player.source_record_id) in proposed)
        for source in (SOFASCORE, UNDERSTAT)
        for player in sorted(held[source], key=lambda p: p.source_record_id)
        if player.source_record_id not in links[source]
    ]
    return resolution


def _by_source(links: dict[RecordKey, int]) -> defaultdict[str, dict[int, int]]:
    """Each source's links: the Player of each Source Record."""
    by_source: dict[str, dict[int, int]] = defaultdict(dict)
    for (source, record), player in links.items():
        by_source[source][record] = player
    return by_source


async def _minutes(conn: AsyncConnection[Any], season: int) -> dict[RecordKey, int]:
    """Each Sofascore and Understat player's minutes in the stored Matches of `season`;
    a player with no Appearance in it is left out."""
    minutes: dict[RecordKey, int] = {}
    for source, query in _MINUTES.items():
        cursor = await conn.execute(query, (season,))
        minutes |= {(source, player): total for player, total in await cursor.fetchall()}
    return minutes


async def _players(conn: AsyncConnection[Any], query: LiteralString) -> list[SourcePlayer]:
    cursor = await conn.execute(query)
    return [
        SourcePlayer(player_id, name, birth_date)
        for player_id, name, birth_date in await cursor.fetchall()
    ]


# Sofascore names a Season "25/26"; `marts.matches` reads its start year the same way.
_MINUTES: dict[str, LiteralString] = {
    SOFASCORE: """
        SELECT appearances.player_id, sum(appearances.minutes_played)::int
        FROM staging.stg_sofascore__appearances AS appearances
        JOIN staging.stg_sofascore__matches AS matches USING (match_id)
        WHERE split_part(matches.season_year, '/', 1)::int + 2000 = %s
        GROUP BY appearances.player_id
    """,
    UNDERSTAT: """
        SELECT appearances.player_id, sum(appearances.minutes_played)::int
        FROM staging.stg_understat__appearances AS appearances
        JOIN staging.stg_understat__matches AS matches USING (match_id)
        WHERE matches.season = %s
        GROUP BY appearances.player_id
    """,
}


# A source's Appearances in canonical Matches both sources have, for canonical Clubs.
_APPEARANCES: dict[str, LiteralString] = {
    SOFASCORE: """
        SELECT matches.match_id, clubs.club_id, appearances.player_id, appearances.minutes_played
        FROM staging.stg_sofascore__appearances AS appearances
        JOIN marts.matches AS matches ON matches.sofascore_match_id = appearances.match_id
        JOIN identity.club_links AS clubs
            ON clubs.provider = 'sofascore' AND clubs.external_id = appearances.team_id::text
        WHERE matches.understat_match_id IS NOT NULL
    """,
    UNDERSTAT: """
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
    """Fails without retries if the release is not loaded: `fdap load-reep`
    loads it."""
    cursor = await conn.execute(
        "SELECT 1 FROM reference.reep_releases WHERE release = %s", (release,)
    )
    if await cursor.fetchone() is None:
        raise ApplicationError(
            f"Reep release {release} is not loaded; run `fdap load-reep`",
            type="ReepReleaseNotLoaded",
            non_retryable=True,
        )


async def _reep_bridges(conn: AsyncConnection[Any], release: str) -> list[ReepBridge]:
    """The release's person bridges, for every provider we ingest."""
    await _require_release(conn, release)
    cursor = await conn.execute(
        """
        SELECT reep_id, provider, external_id::bigint, rule_id, route_scope
        FROM reference.reep_player_bridges
        WHERE release = %s AND external_id ~ '^[0-9]+$'
        """,
        (release,),
    )
    return [ReepBridge(*row) for row in await cursor.fetchall()]
