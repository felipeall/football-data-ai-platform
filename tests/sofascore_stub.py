"""A synthetic Sofascore web API for tests. No scraped data (ADR 4): every value is made up,
but for the countries' public codes and names.

Event 1001 is a finished Match. Players 11 and 12 (home) and 21 and 22 (away) played; 13 sat
on the bench, so it has no minutes and no heatmap. Player 22 came on late: its heatmap and
rating breakdown answer 404, which is normal and must not fail the Match. Player 11 scores,
and player 21 puts the ball in their own net. Events 1003, 1004 and 1005 are
finished Matches with the same lineups. Event 1002 has not started. Any other event answers
404. Players 11 and 21 are Ivorian (Sofascore spells it `Côte d'Ivoire`), the others English,
under Sofascore's own home-nation code `EN`.

Tournament 17 has four seasons, newest first. Season 501 ("24/25") lists its events on two
pages: 1005, 1004, a postponed 1006 and 1003, then 1003 again and 1001. Season 502 ("25/26")
has one page with 1001. Season 503 ("26/27") has no Match played yet: its first page is 404.
"""

from typing import Any

import httpx

EVENT_ID = 1001
NOT_STARTED_EVENT_ID = 1002
FINISHED_EVENT_IDS = (1001, 1003, 1004, 1005)
TOURNAMENT_ID = 17
SEASONS = ((503, "26/27"), (502, "25/26"), (501, "24/25"), (500, "23/24"))
SEASON_ID = 501
SEASON_EVENT_IDS = (1005, 1004, 1003, 1001)  # finished, newest first, each once
EVENT_PAGES: dict[tuple[int, int], tuple[list[tuple[int, str]], bool]] = {
    (501, 0): (
        [(1005, "finished"), (1004, "finished"), (1006, "postponed"), (1003, "finished")],
        True,
    ),
    (501, 1): ([(1003, "finished"), (1001, "finished")], False),
    (502, 0): ([(1001, "finished")], False),
}
# Home and away team per event, as Sofascore team IDs of real clubs: Arsenal (42) and
# Chelsea (38), each at home once. An event's round is its ID less 1000.
EVENT_TEAMS = {1001: (42, 38), 1003: (38, 42), 1004: (42, 38), 1005: (38, 42)}
PLAYED = (11, 12, 21, 22)
BENCHED = 13
NO_HEATMAP = 22
SCORER = 11
OWN_GOAL_SCORER = 21
HOME_PLAYERS = (11, 12, 13)
BASE = "/api/v1/event"


# Sofascore leaves a zero statistic out of a lineup entry.
SCORER_STATISTICS = {"goals": 1, "totalShots": 1, "expectedGoals": 0.3, "totalPass": 2}


COTE_D_IVOIRE = {"name": "Côte d'Ivoire", "slug": "cote-divoire", "alpha2": "CI", "alpha3": "CIV"}
ENGLAND = {"name": "England", "slug": "england", "alpha2": "EN", "alpha3": "ENG"}
IVORIANS = (11, 21)


def _entry(player_id: int, minutes: int | None, substitute: bool = False) -> dict[str, Any]:
    statistics: dict[str, Any] = {"ratingVersions": {}}
    if minutes is not None:
        statistics |= {"minutesPlayed": minutes, "rating": 7.0}
    if player_id == SCORER:
        statistics |= SCORER_STATISTICS
    return {
        "player": {
            "id": player_id,
            "name": f"Player {player_id}",
            "country": COTE_D_IVOIRE if player_id in IVORIANS else ENGLAND,
        },
        "teamId": player_id // 10,
        "position": "M",
        "substitute": substitute,
        "statistics": statistics,
    }


def _event(event_id: int, status: str) -> dict[str, Any]:
    return {
        "event": {
            "id": event_id,
            "status": {"type": status},
            "tournament": {"uniqueTournament": {"id": TOURNAMENT_ID}},
            "season": {"id": SEASON_ID, "year": "24/25"},
            "roundInfo": {"round": event_id - 1000},
            "startTimestamp": 1_735_689_600 + event_id,
            "homeTeam": {"id": EVENT_TEAMS.get(event_id, (42, 38))[0], "name": "Home FC"},
            "awayTeam": {"id": EVENT_TEAMS.get(event_id, (42, 38))[1], "name": "Away FC"},
            "homeScore": {"current": 2},
            "awayScore": {"current": 1},
        }
    }


LINEUPS = {
    "home": {"players": [_entry(11, 90), _entry(12, 90), _entry(BENCHED, None, substitute=True)]},
    "away": {"players": [_entry(21, 90), _entry(22, 4, substitute=True)]},
}


def _shot(shot_id: int, player_id: int, is_home: bool, **details: Any) -> dict[str, Any]:
    return {
        "id": shot_id,
        "player": {"id": player_id, "name": f"Player {player_id}"},
        "isHome": is_home,
        "time": 10,
        "situation": "assisted",
        "bodyPart": "right-foot",
        "xg": 0.3,
    } | details


def _shotmap(event_id: int) -> list[dict[str, Any]]:
    """Player 11's goal, from 10 metres out and left of centre; Sofascore measures x from
    the goal attacked and y from the left touchline. Then player 21's own goal."""
    coordinates = {"x": 10, "y": 40, "z": 0}
    return [
        _shot(
            event_id * 10 + 1,
            SCORER,
            True,
            shotType="goal",
            goalType="regular",
            xgot=0.6,
            playerCoordinates=coordinates,
        ),
        _shot(
            event_id * 10 + 2,
            OWN_GOAL_SCORER,
            False,
            shotType="goal",
            goalType="own",
            playerCoordinates={"x": 3, "y": 50, "z": 0},
        ),
    ]


def _rating_breakdown(player_id: int) -> dict[str, Any]:
    """Every player's rating breakdown: a completed pass, a failed one (Sofascore leaves out
    `outcome`), a ball carry and a won tackle."""
    is_home = player_id in HOME_PLAYERS
    return {
        "passes": [
            {
                "isHome": is_home,
                "outcome": True,
                "keypass": True,
                "eventActionType": "pass",
                "playerCoordinates": {"x": 40, "y": 70},
                "passEndCoordinates": {"x": 60, "y": 75},
            },
            {
                "isHome": is_home,
                "keypass": False,
                "isLongBall": True,
                "eventActionType": "pass",
                "playerCoordinates": {"x": 20, "y": 50},
                "passEndCoordinates": {"x": 80, "y": 50},
            },
        ],
        "dribbles": [],
        "ball-carries": [
            {
                "isHome": is_home,
                "eventActionType": "ball-carry",
                "playerCoordinates": {"x": 45, "y": 60},
                "passEndCoordinates": {"x": 55, "y": 65},
            }
        ],
        "defensive": [
            {
                "isHome": is_home,
                "outcome": True,
                "keypass": False,
                "eventActionType": "tackle",
                "playerCoordinates": {"x": 30, "y": 20},
            }
        ],
    }


class SofascoreStub:
    """Answers like Sofascore for the events above and records every request it receives."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == f"/api/v1/unique-tournament/{TOURNAMENT_ID}/seasons":
            seasons = [{"id": id, "year": year} for id, year in SEASONS]
            return httpx.Response(200, json={"seasons": seasons})
        tournament = f"/api/v1/unique-tournament/{TOURNAMENT_ID}/season/"
        if path.startswith(tournament) and "/events/last/" in path:
            season, page = path.removeprefix(tournament).split("/events/last/")
            if (int(season), int(page)) not in EVENT_PAGES:
                return _not_found()
            events, has_next = EVENT_PAGES[(int(season), int(page))]
            listed = [_event(id, status)["event"] for id, status in events]
            return httpx.Response(200, json={"events": listed, "hasNextPage": has_next})
        if path == f"{BASE}/{NOT_STARTED_EVENT_ID}":
            return httpx.Response(200, json=_event(NOT_STARTED_EVENT_ID, "notstarted"))
        event_id = next(
            (id for id in FINISHED_EVENT_IDS if f"{path}/".startswith(f"{BASE}/{id}/")), None
        )
        if event_id is None:
            return _not_found()
        match = f"{BASE}/{event_id}"
        if path == match:
            return httpx.Response(200, json=_event(event_id, "finished"))
        if path == f"{match}/lineups":
            return httpx.Response(200, json=LINEUPS)
        if path == f"{match}/shotmap":
            return httpx.Response(200, json={"shotmap": _shotmap(event_id)})
        if path == f"{match}/average-positions":
            return httpx.Response(200, json={"home": [], "away": []})
        if path.startswith(f"{match}/player/{NO_HEATMAP}/"):
            return _not_found()
        for player_id in PLAYED:
            if path == f"{match}/player/{player_id}/heatmap":
                return httpx.Response(200, json={"heatmap": [{"x": 50, "y": 50}]})
            if path == f"{match}/player/{player_id}/rating-breakdown":
                return httpx.Response(200, json=_rating_breakdown(player_id))
        return _not_found()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
