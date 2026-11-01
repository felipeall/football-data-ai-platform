"""A synthetic Sofascore web API for tests. No scraped data (ADR 4): every value is made up.

Event 1001 is a finished Match. Players 11 and 12 (home) and 21 and 22 (away) played; 13 sat
on the bench, so it has no minutes and no heatmap. Player 22 came on late: its heatmap
answers 404, which is normal and must not fail the Match. Events 1003, 1004 and 1005 are
finished Matches with the same lineups. Event 1002 has not started. Any other event answers
404.

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
BASE = "/api/v1/event"


def _entry(player_id: int, minutes: int | None, substitute: bool = False) -> dict[str, Any]:
    statistics: dict[str, Any] = {"ratingVersions": {}}
    if minutes is not None:
        statistics |= {"minutesPlayed": minutes, "rating": 7.0}
    return {
        "player": {"id": player_id, "name": f"Player {player_id}"},
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
            return httpx.Response(200, json={"shotmap": [{"player": {"id": 11}, "xg": 0.3}]})
        if path == f"{match}/average-positions":
            return httpx.Response(200, json={"home": [], "away": []})
        if path == f"{match}/player/{NO_HEATMAP}/heatmap":
            return _not_found()
        for player_id in PLAYED:
            if path == f"{match}/player/{player_id}/heatmap":
                return httpx.Response(200, json={"heatmap": [{"x": 50, "y": 50}]})
            if path == f"{match}/player/{player_id}/rating-breakdown":
                return httpx.Response(200, json={"passes": [], "dribbles": []})
        return _not_found()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
