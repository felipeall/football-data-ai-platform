"""One finished Sofascore Match: every response we need from it, fetched through a transport."""

from dataclasses import dataclass
from typing import Any

import httpx

from football_data_ai_platform.sources.sofascore.api import client, raise_for_failure

# Per Match, after the event and its lineups (per-player statistics: Appearances): the
# shotmap (Shots) and average positions. Per player who appeared: heatmap and rating
# breakdown (Actions).
MATCH_PATHS = ("/shotmap", "/average-positions")
PLAYER_PATHS = ("/heatmap", "/rating-breakdown")


class MatchNotFound(Exception):
    """Sofascore has no such event, or no lineups for it."""


class MatchNotFinished(Exception):
    """The event exists but is not finished, so its data is not final."""


@dataclass(frozen=True)
class MatchResponses:
    """What one Match ingest received: every request path with its status code."""

    event_id: int
    name: str  # "Home v Away"
    # Players with minutes in the Match, in lineup order: home, then away.
    player_ids: tuple[int, ...]
    statuses: dict[str, int]


async def fetch_match(
    transport: httpx.AsyncBaseTransport, event_id: int, per_player: bool = True
) -> MatchResponses:
    """Request everything we need from one finished Match through `transport`.

    Match-level data is the event, lineups, shotmap and average positions. `per_player` adds a
    heatmap and rating breakdown per player who appeared: 64 of a Match's ~68 requests.

    A 404 for the event or its lineups raises `MatchNotFound`. A 404 for the shotmap, average
    positions, or one player's heatmap or rating breakdown is a normal answer (little data,
    few minutes played): it is kept and the Match goes on.
    """
    statuses: dict[str, int] = {}
    async with client(transport) as api:

        async def get(path: str, required: bool = False) -> httpx.Response:
            response = await api.get(path)
            statuses[path] = response.status_code
            raise_for_failure(response)
            if response.status_code == 404 and required:
                raise MatchNotFound(f"Sofascore has no {path}")
            return response

        match = f"/event/{event_id}"
        event = (await get(match, required=True)).json()["event"]
        if event["status"]["type"] != "finished":
            raise MatchNotFinished(f"Event {event_id} is {event['status']['type']}")
        lineups = (await get(f"{match}/lineups", required=True)).json()
        for path in MATCH_PATHS:
            await get(f"{match}{path}")
        player_ids = _appeared(lineups)
        for player_id in player_ids if per_player else ():
            for path in PLAYER_PATHS:
                await get(f"{match}/player/{player_id}{path}")

    return MatchResponses(
        event_id=event_id,
        name=f"{event['homeTeam']['name']} v {event['awayTeam']['name']}",
        player_ids=player_ids,
        statuses=statuses,
    )


def _appeared(lineups: dict[str, Any]) -> tuple[int, ...]:
    """Players with minutes played. Unused substitutes have no heatmap or rating breakdown."""
    return tuple(
        entry["player"]["id"]
        for side in ("home", "away")
        for entry in lineups[side]["players"]
        if entry.get("statistics", {}).get("minutesPlayed")
    )


@dataclass(frozen=True)
class MatchStatus:
    # Sofascore's status type: "notstarted", "inprogress", "finished", "postponed",
    # "canceled", …
    status: str
    start_timestamp: int  # current kickoff, Unix seconds


async def fetch_match_status(transport: httpx.AsyncBaseTransport, event_id: int) -> MatchStatus:
    """Where a Match stands now, and its current kickoff. A 404 raises `MatchNotFound`."""
    async with client(transport) as api:
        response = await api.get(f"/event/{event_id}")
    raise_for_failure(response)
    if response.status_code == 404:
        raise MatchNotFound(f"Sofascore has no event {event_id}")
    event = response.json()["event"]
    return MatchStatus(event["status"]["type"], event["startTimestamp"])
