"""A synthetic Understat. No scraped data (ADR 4): every value is made up.

Season 2024 (24/25) lists three fixtures: Matches 1 and 2 were played, 3 was not. Season
2025 (25/26) lists Match 4, played. Season 2026 has no data: 404. Match 2's data answers
404 although the league data lists it as played. Like Understat, every request without
`X-Requested-With: XMLHttpRequest` answers 404 with an HTML page.
"""

from typing import Any

import httpx

LEAGUE = "EPL"
FIXTURES: dict[int, list[tuple[int, bool]]] = {
    2024: [(1, True), (2, True), (3, False)],
    2025: [(4, True)],
}
MISSING_MATCH_ID = 2
# Understat team IDs of real clubs: Arsenal (83) and Chelsea
# (80), each at home once in 2024.
ARSENAL, CHELSEA = 83, 80
MATCH_TEAMS = {
    1: (ARSENAL, CHELSEA),
    2: (CHELSEA, ARSENAL),
    3: (ARSENAL, 88),
    4: (ARSENAL, CHELSEA),
}
# Understat names players itself; IDs 801-804 are made up. In every Match, 801 scores from
# 802's pass, and 803 puts the ball in their own net.
ROSTER = {"h": [(801, 90), (802, 75)], "a": [(803, 90), (804, 15)]}


def _teams(match_id: int) -> tuple[int, int]:
    return MATCH_TEAMS.get(match_id, (ARSENAL, CHELSEA))


def _fixture(match_id: int, played: bool) -> dict[str, Any]:
    return {
        "id": str(match_id),
        "isResult": played,
        "h": {"id": str(_teams(match_id)[0]), "title": "Home FC", "short_title": "HOM"},
        "a": {"id": str(_teams(match_id)[1]), "title": "Away FC", "short_title": "AWA"},
        "goals": {"h": "2", "a": "1"} if played else {"h": None, "a": None},
        "xG": {"h": "1.5", "a": "0.75"} if played else {"h": None, "a": None},
        "datetime": f"2025-01-0{match_id} 15:00:00",
    }


def match_data(match_id: int) -> dict[str, Any]:
    rosters = {
        side: {
            str(match_id * 1000 + player_id): {
                "id": str(match_id * 1000 + player_id),
                "player_id": str(player_id),
                "player": f"Player {player_id}",
                "team_id": str(_teams(match_id)[0 if side == "h" else 1]),
                "h_a": side,
                "time": str(minutes),
                "position": "MC",
                "goals": "0",
                "shots": "1",
                "xG": "0.25",
                "xA": "0",
                "key_passes": "0",
                "assists": "0",
                "xGChain": "0.25",
                "xGBuildup": "0",
            }
            for player_id, minutes in players
        }
        for side, players in ROSTER.items()
    }
    shot = {
        "id": str(match_id * 10),
        "minute": "10",
        "result": "Goal",
        "X": "0.9",
        "Y": "0.5",
        "xG": "0.25",
        "player": "Player 801",
        "player_id": "801",
        "h_a": "h",
        "situation": "OpenPlay",
        "shotType": "RightFoot",
        "match_id": str(match_id),
        "player_assisted": "Player 802",
        "lastAction": "Pass",
    }
    own_goal = shot | {
        "id": str(match_id * 10 + 1),
        "minute": "50",
        "result": "OwnGoal",
        "X": "0.03",
        "player": "Player 803",
        "player_id": "803",
        "h_a": "a",
        "player_assisted": None,
    }
    return {"rosters": rosters, "shots": {"h": [shot], "a": [own_goal]}, "tmpl": {}}


class UnderstatStub:
    """Answers like Understat for the seasons above and records every request it receives."""

    def __init__(self, fixtures: dict[int, list[tuple[int, bool]]] = FIXTURES) -> None:
        self.fixtures = fixtures
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return _not_found()
        path = request.url.path
        for year, fixtures in self.fixtures.items():
            if path == f"/getLeagueData/{LEAGUE}/{year}":
                dates = [_fixture(match_id, played) for match_id, played in fixtures]
                return httpx.Response(200, json={"teams": {}, "players": [], "dates": dates})
        played = {m for fixtures in self.fixtures.values() for m, p in fixtures if p}
        for match_id in played - {MISSING_MATCH_ID}:
            if path == f"/getMatchData/{match_id}":
                return httpx.Response(200, json=match_data(match_id))
        return _not_found()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def _not_found() -> httpx.Response:
    return httpx.Response(404, html="<!DOCTYPE html><html><body>404</body></html>")
