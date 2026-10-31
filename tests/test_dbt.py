import os
import subprocess
import sys
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict

from football_intel.fetch_attempts import FetchAttemptStore
from football_intel.raw_responses import RawResponseStore
from football_intel.sources import sofascore, transfermarkt, understat
from football_intel.sources.sofascore.matches import MatchNotFinished, fetch_match
from football_intel.sources.transfermarkt.squads import fetch_squad
from football_intel.sources.understat import league as understat_league
from tests.sofascore_stub import NOT_STARTED_EVENT_ID, SofascoreStub
from tests.transfermarkt_stub import CLUB_ID, PLAYERS, TransfermarktStub
from tests.understat_stub import ARSENAL, CHELSEA, LEAGUE, UnderstatStub

DBT_PROJECT = Path(__file__).parent.parent / "dbt"


def _dbt_build(database_url: str, tmp_path: Path) -> None:
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
    args = ["--project-dir", DBT_PROJECT, "--profiles-dir", DBT_PROJECT]
    paths = ["--target-path", tmp_path / "target", "--log-path", tmp_path / "logs"]
    result = subprocess.run(
        [dbt, "build", *args, *paths], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout[-4000:]


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


async def test_dbt_links_matches_across_sources_by_season_and_clubs(
    connection: AsyncConnection[Any], database_url: str, tmp_path: Path
) -> None:
    await _store_matches(connection)
    recording = understat.recording_transport(
        UnderstatStub().transport(), RawResponseStore(connection), FetchAttemptStore(connection)
    )
    await understat_league.fetch_league_season(recording, LEAGUE, 2024)

    _dbt_build(database_url, tmp_path)

    cursor = await connection.execute(
        "SELECT match_id, season, home_club_id, away_club_id, sofascore_match_id,"
        " understat_match_id FROM marts.matches ORDER BY match_id"
    )
    # Sofascore names the season "24/25", Understat 2024; their team IDs differ. The Clubs
    # and the season link them, so each fixture is one Match with both IDs.
    assert await cursor.fetchall() == [
        ("2024:arsenal:chelsea", 2024, "arsenal", "chelsea", 1001, 1),
        ("2024:chelsea:arsenal", 2024, "chelsea", "arsenal", 1003, 2),
    ]


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
            moved = PLAYERS["101"] | {}
            moved["clubAssignments"] = [{"type": "current", "clubId": self.NEW_CLUB_ID}]
            players = PLAYERS | ({"101": moved} if self.players_answered > 1 else {})
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
