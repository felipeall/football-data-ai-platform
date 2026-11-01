import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from football_data_ai_platform.fetch_attempts import FetchAttemptStore
from football_data_ai_platform.identity.activities import ClubResult, IdentityActivities
from football_data_ai_platform.identity.store import ClubStore
from football_data_ai_platform.raw_responses import RawResponseStore
from football_data_ai_platform.sources import sofascore, transfermarkt, understat
from football_data_ai_platform.sources.sofascore.matches import MatchNotFinished, fetch_match
from football_data_ai_platform.sources.transfermarkt.squads import fetch_squad
from football_data_ai_platform.sources.understat import league as understat_league
from football_data_ai_platform.transform.dbt import (
    CONNECTION_ERRORS,
    DbtActivities,
    lost_connection,
)
from football_data_ai_platform.transform.workflows import (
    CLUBS_AND_MATCHES,
    STAGING,
    THE_REST,
    TransformWorkflow,
)
from tests.sofascore_stub import NOT_STARTED_EVENT_ID, SofascoreStub
from tests.transfermarkt_stub import ATTRIBUTES, CLUB_ID, PLAYERS, SEASON_ID, TransfermarktStub
from tests.understat_stub import ARSENAL, CHELSEA, LEAGUE, UnderstatStub

DBT_PROJECT = Path(__file__).parent.parent / "dbt"
# The stubs' Matches are 24/25.
COVERED_SEASON = 2024


def _dbt_build(
    database_url: str, tmp_path: Path, *selection: str, succeeds: bool = True
) -> subprocess.CompletedProcess[str]:
    """`dbt build` against the test database, as a subprocess, as it runs from the CLI."""
    conninfo = conninfo_to_dict(database_url)
    env = os.environ | {
        "POSTGRES_HOST": str(conninfo["host"]),
        "POSTGRES_PORT": str(conninfo["port"]),
        "POSTGRES_DB": str(conninfo["dbname"]),
        "POSTGRES_USER": str(conninfo["user"]),
        "POSTGRES_PASSWORD": str(conninfo.get("password", "")),
    }
    dbt = Path(sys.executable).parent / "dbt"
    args = ["--no-use-colors", "--project-dir", DBT_PROJECT, "--profiles-dir", DBT_PROJECT]
    paths = ["--target-path", tmp_path / "target", "--log-path", tmp_path / "logs"]
    result = subprocess.run(
        [dbt, "build", *args, *paths, *selection],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == succeeds, result.stdout[-4000:]
    return result


async def _store_matches(connection: AsyncConnection[Any]) -> None:
    """Two finished Matches, one of them fetched twice, and a status check of a Match not
    started: what post-match workflows and re-runs leave in `raw.raw_responses`."""
    stub = SofascoreStub()
    recording = sofascore.recording_transport(
        stub.transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )
    for event_id in (1001, 1003, 1001):
        await fetch_match(recording, event_id, per_player=False)
    with pytest.raises(MatchNotFinished):
        await fetch_match(recording, NOT_STARTED_EVENT_ID, per_player=False)
    # A later challenge for a stored event must not hide its last successful answer.
    await connection.execute(
        "INSERT INTO raw.raw_responses (source, method, url, status_code, headers, body,"
        " body_json, captured_at)"
        " SELECT source, method, url, 403, '{}', '', '{\"error\": {\"reason\": \"challenge\"}}',"
        " captured_at + interval '1 hour' FROM raw.raw_responses WHERE url LIKE '%/event/1003'"
    )


async def test_dbt_builds_matches_appearances_and_season_minutes(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    await _store_matches(connection)

    _dbt_build(database_url, tmp_path)

    cursor = await connection.execute(
        "SELECT match_id, season_year, home_team_id, away_team_id, home_goals, away_goals"
        " FROM staging.stg_sofascore__matches ORDER BY match_id"
    )
    # A re-fetched Match counts once, and a Match not started is not a Match yet.
    assert await cursor.fetchall() == [
        (1001, "24/25", 42, 38, 2, 1),
        (1003, "24/25", 38, 42, 2, 1),
    ]
    cursor = await connection.execute(
        "SELECT player_id, team_id FROM staging.stg_sofascore__appearances"
        " WHERE match_id = 1001 ORDER BY player_id"
    )
    # The unused substitute has no minutes, so no Appearance. Home players take the home team.
    assert await cursor.fetchall() == [(11, 42), (12, 42), (21, 38), (22, 38)]
    cursor = await connection.execute(
        "SELECT player_id, season_year, appearances, minutes_played"
        " FROM marts.player_season_minutes ORDER BY player_id"
    )
    assert await cursor.fetchall() == [
        (11, "24/25", 2, 180),
        (12, "24/25", 2, 180),
        (21, "24/25", 2, 180),
        (22, "24/25", 2, 8),
    ]


async def test_dbt_builds_understat_matches_appearances_and_shots(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    recording = understat.recording_transport(
        UnderstatStub().transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )
    season = await understat_league.fetch_league_season(recording, LEAGUE, 2024)
    for match_id in season.finished_match_ids:
        with suppress(understat_league.MatchDataMissing):
            await understat_league.fetch_match(recording, match_id)

    _dbt_build(database_url, tmp_path)

    cursor = await connection.execute(
        "SELECT match_id, season, season_year, home_goals, away_goals, home_xg"
        " FROM staging.stg_understat__matches ORDER BY match_id"
    )
    # Match 3 is not played yet; Match 2 is played, though its data is missing.
    assert await cursor.fetchall() == [
        (1, 2024, "24/25", 2, 1, Decimal("1.5")),
        (2, 2024, "24/25", 2, 1, Decimal("1.5")),
    ]
    cursor = await connection.execute(
        "SELECT match_id, player_id, team_id, side, minutes_played"
        " FROM staging.stg_understat__appearances ORDER BY player_id"
    )
    assert await cursor.fetchall() == [
        (1, 801, ARSENAL, "home", 90),
        (1, 802, ARSENAL, "home", 75),
        (1, 803, CHELSEA, "away", 90),
        (1, 804, CHELSEA, "away", 15),
    ]
    cursor = await connection.execute(
        "SELECT match_id, player_id, side, x, y, xg FROM staging.stg_understat__shots"
    )
    assert await cursor.fetchall() == [
        (1, 801, "home", Decimal("0.9"), Decimal("0.5"), Decimal("0.25"))
    ]


# Made-up Reep teams bridging the stubs' teams (ADR 4): Arsenal is Sofascore 42, Understat
# 83 and Transfermarkt 11; Chelsea is Sofascore 38, Understat 80 and Transfermarkt 631.
REEP_TEAM_BRIDGES = [
    ("rt-arsenal", "sofascore", "42"),
    ("rt-arsenal", "understat", str(ARSENAL)),
    ("rt-arsenal", "transfermarkt", "11"),
    ("rt-chelsea", "sofascore", "38"),
    ("rt-chelsea", "understat", str(CHELSEA)),
    ("rt-chelsea", "transfermarkt", "631"),
]
TRANSFERMARKT_CLUBS = [
    {"id": "11", "name": "Arsenal FC", "baseDetails": {"shortName": "Arsenal", "countryId": 1}},
    {"id": "631", "name": "Chelsea FC", "baseDetails": {"shortName": "", "countryId": 1}},
]
TM_API = "https://tmapi.transfermarkt.technology"


async def _store_json(connection: AsyncConnection[Any], source: str, url: str, body: Any) -> None:
    await connection.execute(
        "INSERT INTO raw.raw_responses (source, method, url, status_code, headers, body,"
        " body_json, captured_at) VALUES (%s, 'GET', %s, 200, '{}', %s, %s, now())",
        (source, url, json.dumps(body).encode(), Jsonb(body)),
    )


async def _store_reep_teams_and_club_names(
    connection: AsyncConnection[Any], bridges: list[tuple[str, str, str]] = REEP_TEAM_BRIDGES
) -> None:
    """A made-up Reep release with team `bridges`, and Transfermarkt's club names."""
    await connection.execute("DELETE FROM reference.reep_releases WHERE release = 'test'")
    await connection.execute(
        "INSERT INTO reference.reep_releases (release, manifest) VALUES ('test', '{}')"
    )
    for reep_id, provider, external_id in bridges:
        await connection.execute(
            "INSERT INTO reference.reep_team_bridges (release, reep_id, provider, external_id)"
            " VALUES ('test', %s, %s, %s)",
            (reep_id, provider, external_id),
        )
    await _store_json(
        connection,
        "transfermarkt",
        f"{TM_API}/clubs?ids%5B%5D=11&ids%5B%5D=631",
        {"success": True, "data": TRANSFERMARKT_CLUBS},
    )
    await _store_json(connection, "transfermarkt", f"{TM_API}/attributes", {"data": ATTRIBUTES})


async def _create_clubs(
    connection: AsyncConnection[Any],
    database_url: str,
    tmp_path: Path,
    bridges: list[tuple[str, str, str]] = REEP_TEAM_BRIDGES,
) -> ClubResult:
    """Steps 1 and 2 of the Transform Run: dbt staging, then the Club step."""
    await _store_reep_teams_and_club_names(connection, bridges)
    _dbt_build(database_url, tmp_path, "--select", "staging")
    return await ActivityEnvironment().run(
        IdentityActivities(database_url, "test", covered_season=COVERED_SEASON).create_clubs
    )


# The stub's events all kick off on 1 January; these are Understat's kickoffs of the same
# fixtures, so the two sides of a Match agree, as they do live.
KICKOFFS = {1001: "2025-01-01 15:00Z", 1003: "2025-01-02 15:00Z"}


async def _kick_off(connection: AsyncConnection[Any], event_id: int, at: str) -> None:
    await connection.execute(
        "UPDATE raw.raw_responses SET body_json = jsonb_set(body_json,"
        " '{event,startTimestamp}', to_jsonb(extract(epoch FROM %s::timestamptz)::bigint))"
        " WHERE url LIKE %s",
        (at, f"%/event/{event_id}"),
    )


async def _store_both_sources(connection: AsyncConnection[Any], *event_ids: int) -> None:
    stub = SofascoreStub()
    recording = sofascore.recording_transport(
        stub.transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )
    for event_id in event_ids:
        await fetch_match(recording, event_id, per_player=False)
        await _kick_off(connection, event_id, KICKOFFS[event_id])
    recording = understat.recording_transport(
        UnderstatStub().transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )
    await understat_league.fetch_league_season(recording, LEAGUE, 2024)


async def test_dbt_links_matches_across_sources_by_competition_season_and_clubs(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    await _store_both_sources(connection, 1001, 1003)

    result = await _create_clubs(connection, database_url, tmp_path)
    _dbt_build(database_url, tmp_path)

    assert (result.created, result.links_added, result.unresolved) == (
        ["arsenal", "chelsea"],
        8,
        [],
    )
    cursor = await connection.execute(
        "SELECT club_id, display_name, country, sofascore_team_id, understat_team_id,"
        " transfermarkt_club_id, reep_team_id FROM marts.clubs ORDER BY club_id"
    )
    # Chelsea's short name is empty, so it is displayed by its full name.
    assert await cursor.fetchall() == [
        ("arsenal", "Arsenal", "Testland", 42, ARSENAL, 11, "rt-arsenal"),
        ("chelsea", "Chelsea FC", "Testland", 38, CHELSEA, 631, "rt-chelsea"),
    ]
    cursor = await connection.execute(
        "SELECT match_id, round, sofascore_match_id, understat_match_id FROM marts.matches"
        " ORDER BY match_id"
    )
    # Sofascore names the season "24/25", Understat 2024; their team IDs differ. The Clubs,
    # the Competition and the season link them, so each fixture is one Match with both IDs.
    assert await cursor.fetchall() == [
        ("premier-league:2024:league:arsenal:chelsea", 1, 1001, 1),
        ("premier-league:2024:league:chelsea:arsenal", 3, 1003, 2),
    ]

    # A re-run creates nothing and changes nothing.
    again = await ActivityEnvironment().run(
        IdentityActivities(database_url, "test", covered_season=COVERED_SEASON).create_clubs
    )
    assert (again.created, again.links_added, again.clubs) == ([], 0, 2)


async def test_dbt_leaves_out_the_matches_of_an_unlinked_team_with_a_warning(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    await _store_both_sources(connection, 1001, 1003)
    # Reep bridges Chelsea's Sofascore team only, not its Understat team.
    bridges = [b for b in REEP_TEAM_BRIDGES if b[1:] != ("understat", str(CHELSEA))]

    result = await _create_clubs(connection, database_url, tmp_path, bridges)
    dbt = DbtActivities(database_url, DBT_PROJECT)
    await ActivityEnvironment().run(dbt.build_dbt, CLUBS_AND_MATCHES)
    build = await ActivityEnvironment().run(dbt.build_dbt, THE_REST)

    assert [(t.provider, t.external_id) for t in result.unresolved] == [("understat", str(CHELSEA))]
    cursor = await connection.execute(
        "SELECT match_id, sofascore_match_id, understat_match_id FROM marts.matches"
        " ORDER BY match_id"
    )
    assert await cursor.fetchall() == [
        ("premier-league:2024:league:arsenal:chelsea", 1001, None),
        ("premier-league:2024:league:chelsea:arsenal", 1003, None),
    ]
    # The Transform Run reports the warning, and goes on.
    assert len(build.warnings) == 1, build.warnings
    assert "WARN 2 warn_matches_left_out_of_the_marts " in build.warnings[0]


async def test_a_renamed_clubs_former_match_ids_still_resolve(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    await _store_both_sources(connection, 1001)
    await _create_clubs(connection, database_url, tmp_path)

    await ClubStore(connection).rename("arsenal", "the-arsenal")
    _dbt_build(database_url, tmp_path)

    cursor = await connection.execute(
        "SELECT alias, match_id FROM marts.match_id_aliases ORDER BY alias"
    )
    assert await cursor.fetchall() == [
        (
            "premier-league:2024:league:arsenal:chelsea",
            "premier-league:2024:league:the-arsenal:chelsea",
        ),
        (
            "premier-league:2024:league:chelsea:arsenal",
            "premier-league:2024:league:chelsea:the-arsenal",
        ),
    ]


async def test_a_stage_link_names_the_stage_of_a_paired_match(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    await _store_both_sources(connection, 1001, 1003)
    await connection.execute(
        "INSERT INTO identity.stages (competition_id, stage_id, name)"
        " VALUES ('premier-league', 'run-in', 'Run-in')"
    )
    await connection.execute(
        "INSERT INTO identity.stage_links (competition_id, stage_id, provider, first_round,"
        " last_round) VALUES ('premier-league', 'run-in', 'sofascore', 3, 38)"
    )
    try:
        await _create_clubs(connection, database_url, tmp_path)
        _dbt_build(database_url, tmp_path)

        cursor = await connection.execute(
            "SELECT match_id, sofascore_match_id, understat_match_id FROM marts.matches"
            " ORDER BY match_id"
        )
        # Understat has no round, so Match 2 takes its Stage from the Sofascore side.
        assert await cursor.fetchall() == [
            ("premier-league:2024:league:arsenal:chelsea", 1001, 1),
            ("premier-league:2024:run-in:chelsea:arsenal", 1003, 2),
        ]
    finally:
        await connection.execute("DELETE FROM identity.stage_links")
        await connection.execute("DELETE FROM identity.stages WHERE stage_id = 'run-in'")


async def test_dbt_fails_when_one_fixture_is_two_matches_with_home_and_away_swapped(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    # Sofascore has Chelsea v Arsenal on 1 January, paired with Understat's Match 2. Understat
    # also has Arsenal v Chelsea on 1 January (Match 1): the same fixture, sides swapped.
    await _store_both_sources(connection, 1003)
    await _kick_off(connection, 1003, KICKOFFS[1001])
    await _create_clubs(connection, database_url, tmp_path)

    build = _dbt_build(database_url, tmp_path, succeeds=False)

    failures = [line for line in build.stdout.splitlines() if " FAIL " in line]
    assert len(failures) == 1, build.stdout[-4000:]
    assert "assert_no_fixture_recorded_twice" in failures[0]


SQUADS_BY_SEASON = {"2025": ("101", "102"), "2024": ("101", "103")}


class TwoSeasonsStub(TransfermarktStub):
    """Player 101 is in club 1's squad in two seasons, next to a different teammate each
    season, so each season's batched `/players` request has its own URL. Between the two
    fetches the player moves: the later answer has them at club 4."""

    NEW_CLUB_ID = 4

    def __init__(self) -> None:
        super().__init__()
        self.players_answered = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/club/{CLUB_ID}/squad":
            ids = SQUADS_BY_SEASON[request.url.params["season"]]
            members = [
                {"playerId": int(i), "type": "historical", "shirtNumber": n, "isCaptain": False}
                for n, i in enumerate(ids, start=1)
            ]
            return httpx.Response(200, json={"success": True, "data": {"squad": members}})
        if path == "/players":
            self.players_answered += 1
            players = dict(PLAYERS)
            if self.players_answered > 1:
                moved = [{"type": "current", "clubId": self.NEW_CLUB_ID}]
                players["101"] = PLAYERS["101"] | {"clubAssignments": moved}
            ids = request.url.params.get_list("ids[]")
            return httpx.Response(
                200, json={"success": True, "data": [players[i] for i in ids if i in players]}
            )
        return super().handle(request)


async def test_dbt_keeps_one_transfermarkt_player_across_seasons(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    stub = TwoSeasonsStub()
    recording = transfermarkt.recording_transport(
        stub.transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )
    # The older season is fetched last: the latest response, not the latest season, holds
    # a player's current club.
    await fetch_squad(recording, CLUB_ID, "2025")
    await fetch_squad(recording, CLUB_ID, "2024")

    _dbt_build(database_url, tmp_path)

    cursor = await connection.execute(
        "SELECT player_id, current_club_id FROM staging.stg_transfermarkt__players"
        " ORDER BY player_id"
    )
    assert await cursor.fetchall() == [
        (101, TwoSeasonsStub.NEW_CLUB_ID),
        (102, int(CLUB_ID)),
        (103, int(CLUB_ID)),
    ]


async def test_dbt_never_writes_to_the_schemas_alembic_owns(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    # A dbt relation in one of these schemas makes the next migration that creates or
    # changes that schema fail against a database dbt has already built (ADR 31).
    owned = """
        SELECT table_schema, table_name FROM information_schema.tables
        WHERE table_schema IN ('raw', 'identity', 'reference')
        ORDER BY 1, 2
    """
    before = await (await connection.execute(owned)).fetchall()

    _dbt_build(database_url, tmp_path)

    assert await (await connection.execute(owned)).fetchall() == before


async def test_dbt_fails_when_a_candidate_s_source_record_is_not_staged(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    # #74: a whole season's Raw Responses were deleted, and 252 Players kept links to
    # Sofascore records no lineup held any more. The build must say so.
    await _store_matches(connection)
    recording = understat.recording_transport(
        UnderstatStub().transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )
    await understat_league.fetch_league_season(recording, LEAGUE, 2024)
    await understat_league.fetch_match(recording, 1)
    await fetch_squad(
        transfermarkt.recording_transport(
            TransfermarktStub().transport(),
            RawResponseStore(connection),
            FetchAttemptStore(connection),
        ),
        CLUB_ID,
        SEASON_ID,
    )
    # Sofascore 11, Understat 801 and Transfermarkt 101 are in stored responses; none
    # holds 999.
    candidates = [
        ("sofascore", "11"),
        ("sofascore", "999"),
        ("understat", "801"),
        ("understat", "999"),
        ("transfermarkt", "101"),
        ("transfermarkt", "999"),
    ]
    for source, source_record_id in candidates:
        cursor = await connection.execute(
            "INSERT INTO identity.players DEFAULT VALUES RETURNING player_id"
        )
        row = await cursor.fetchone()
        assert row is not None
        await connection.execute(
            "INSERT INTO identity.identity_candidates"
            " (source, source_record_id, player_id, method, confidence, status)"
            " VALUES (%s, %s, %s, 'test', 1, 'pending')",
            (source, source_record_id, row[0]),
        )

    result = _dbt_build(database_url, tmp_path, succeeds=False)

    # Only this test fails, and only for the three records no stored response holds.
    assert "ERROR=1 " in result.stdout, result.stdout[-4000:]
    assert re.search(
        r"in test assert_identity_candidates_have_staged_source_records .*\n.*Got 3 results,",
        result.stdout,
    ), result.stdout[-4000:]


async def test_identity_lists_only_the_unlinked_records_that_played_in_the_covered_season(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    await _store_both_sources(connection, 1001, 1003)
    recording = understat.recording_transport(
        UnderstatStub().transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )
    await understat_league.fetch_match(recording, 1)
    # Match 1003 moves to 25/26, without player 22, who then played in 24/25 only.
    await connection.execute(
        "UPDATE raw.raw_responses SET body_json = jsonb_set(body_json, '{event,season,year}',"
        " '\"25/26\"') WHERE url LIKE '%/event/1003'"
    )
    await connection.execute(
        "UPDATE raw.raw_responses SET body_json = jsonb_set(body_json, '{away,players}',"
        " (SELECT jsonb_agg(entry) FROM jsonb_array_elements(body_json -> 'away' -> 'players')"
        " AS entry WHERE entry -> 'player' ->> 'id' <> '22'))"
        " WHERE url LIKE '%/event/1003/lineups'"
    )
    await _create_clubs(connection, database_url, tmp_path)
    await ActivityEnvironment().run(
        DbtActivities(database_url, DBT_PROJECT).build_dbt, CLUBS_AND_MATCHES
    )

    async def unlinked(covered_season: int) -> list[tuple[str, int, int]]:
        identity = IdentityActivities(database_url, "test", covered_season=covered_season)
        result = await ActivityEnvironment().run(identity.resolve_identities)
        assert result.covered_season == covered_season
        return [(r.source, r.source_record_id, r.minutes) for r in result.unlinked]

    # No Transfermarkt squad is stored, so no record links. In 25/26 only Sofascore 1003 was
    # played: 22 and the Understat players played in 24/25 only, and 13 never played.
    assert await unlinked(2025) == [
        ("sofascore", 11, 90),
        ("sofascore", 12, 90),
        ("sofascore", 21, 90),
    ]
    # Each source's minutes count only the covered Season's Matches.
    assert await unlinked(2024) == [
        ("sofascore", 11, 90),
        ("sofascore", 12, 90),
        ("sofascore", 21, 90),
        ("sofascore", 22, 4),
        ("understat", 801, 90),
        ("understat", 802, 75),
        ("understat", 803, 90),
        ("understat", 804, 15),
    ]


async def test_a_transform_run_builds_every_model_from_empty_staging_and_marts(
    temporal: WorkflowEnvironment, connection: AsyncConnection[Any], database_url: str
) -> None:
    # Step 3 must not run a test of a model only step 4 builds (`match_id_aliases`).
    await connection.execute("DROP SCHEMA IF EXISTS staging, marts CASCADE")
    await _store_both_sources(connection, 1001, 1003)
    await _store_reep_teams_and_club_names(connection)
    identity = IdentityActivities(database_url, "test", covered_season=COVERED_SEASON)
    task_queue = f"test-{uuid.uuid4()}"
    async with Worker(
        temporal.client,
        task_queue=task_queue,
        workflows=[TransformWorkflow],
        activities=[
            DbtActivities(database_url, DBT_PROJECT).build_dbt,
            identity.create_clubs,
            identity.resolve_identities,
        ],
    ):
        result = await temporal.client.execute_workflow(
            TransformWorkflow.run, id=f"transform-{uuid.uuid4()}", task_queue=task_queue
        )

    assert result.clubs.created == ["arsenal", "chelsea"]
    cursor = await connection.execute("SELECT match_id FROM marts.matches ORDER BY match_id")
    assert await cursor.fetchall() == [
        ("premier-league:2024:league:arsenal:chelsea",),
        ("premier-league:2024:league:chelsea:arsenal",),
    ]
    # Built in step 4; empty, since no Club was renamed.
    cursor = await connection.execute("SELECT count(*) FROM marts.match_id_aliases")
    assert await cursor.fetchone() == (0,)
    for build in (result.staging, result.clubs_and_matches, result.the_rest):
        assert " ERROR=0 " in build.summary, build.summary


async def test_a_failed_dbt_test_fails_the_build_without_retries(
    connection: AsyncConnection[Any], database_url: str
) -> None:
    dbt = DbtActivities(database_url, DBT_PROJECT)
    await _store_matches(connection)
    await ActivityEnvironment().run(dbt.build_dbt, STAGING)
    cursor = await connection.execute(
        "INSERT INTO identity.players DEFAULT VALUES RETURNING player_id"
    )
    row = await cursor.fetchone()
    assert row is not None
    # No stored response holds Sofascore player 999.
    await connection.execute(
        "INSERT INTO identity.identity_candidates"
        " (source, source_record_id, player_id, method, confidence, status)"
        " VALUES ('sofascore', '999', %s, 'test', 1, 'pending')",
        (row[0],),
    )

    # Step 4 runs this test, which reads staging and `identity` both.
    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(dbt.build_dbt, THE_REST)

    assert failure.value.non_retryable
    assert "assert_identity_candidates_have_staged_source_records" in failure.value.message


async def test_a_build_that_lost_its_database_connection_is_retried(
    connection: AsyncConnection[Any], database_url: str
) -> None:
    dbt = DbtActivities(database_url, DBT_PROJECT)
    await _store_matches(connection)
    # Reading the Raw Responses ends the reader's own connection, as a Postgres restart would.
    await connection.execute("ALTER TABLE raw.raw_responses RENAME TO raw_responses_kept")
    await connection.execute(
        "CREATE VIEW raw.raw_responses AS SELECT r.* FROM raw.raw_responses_kept r"
        " WHERE pg_terminate_backend(pg_backend_pid())"
    )
    try:
        with pytest.raises(ApplicationError) as failure:
            await ActivityEnvironment().run(dbt.build_dbt, STAGING)
    finally:
        # With the staging views dbt built on it; the next build recreates them.
        await connection.execute("DROP VIEW raw.raw_responses CASCADE")
        await connection.execute("ALTER TABLE raw.raw_responses_kept RENAME TO raw_responses")

    assert "exited 1" in failure.value.message, failure.value.message
    assert not failure.value.non_retryable, failure.value.message
    assert "server closed the connection unexpectedly" in failure.value.message


def _results(*failures: tuple[str, str]) -> dict[str, Any]:
    """A `run_results.json` with one passing model and `failures`, each (status, message)."""
    results: list[dict[str, Any]] = [
        {"unique_id": "model.x.passed", "status": "success", "message": "SELECT 1"}
    ]
    results += [
        {"unique_id": f"node.x.failed_{n}", "status": status, "message": message}
        for n, (status, message) in enumerate(failures)
    ]
    results.append({"unique_id": "model.x.skipped", "status": "skipped", "message": None})
    return {"metadata": {"invocation_id": "x"}, "results": results}


@pytest.mark.parametrize(
    "error",
    [
        *CONNECTION_ERRORS,
        'connection to server at "postgres" (172.18.0.2), port 5432 failed: Connection refused',
    ],
)
def test_a_lost_connection_may_pass_when_run_again(error: str) -> None:
    message = f"Database Error in model staging_x (models/staging_x.sql)\n  {error}\n"
    assert lost_connection(_results(("error", message), ("error", message)))


@pytest.mark.parametrize(
    "run_results",
    [
        None,  # dbt wrote no results
        _results(),  # nothing failed
        _results(("error", 'Database Error\n  relation "raw.raw_responses" does not exist')),
        _results(("fail", "Got 3 results, configured to fail if != 0")),
        # A real failure next to a lost connection fails the same way again.
        _results(
            ("error", "Database Error\n  server closed the connection unexpectedly"),
            ("fail", "Got 3 results, configured to fail if != 0"),
        ),
        # A test whose failing rows mention a lost connection is still a failing test.
        _results(("fail", "server closed the connection unexpectedly")),
    ],
)
def test_any_other_failure_would_fail_again(run_results: dict[str, Any] | None) -> None:
    assert not lost_connection(run_results)
