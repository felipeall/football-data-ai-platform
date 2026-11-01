"""A tournament's seasons and each season's finished Matches, from Sofascore."""

from dataclasses import dataclass

import httpx

from football_data_ai_platform.sources.sofascore.api import client, raise_for_failure

PREMIER_LEAGUE = 17  # Sofascore's unique-tournament ID


class SeasonNotFound(Exception):
    """The tournament has no season with that year."""


@dataclass(frozen=True)
class Season:
    id: int
    year: str  # as Sofascore writes it, e.g. "25/26"


async def fetch_seasons(transport: httpx.AsyncBaseTransport, tournament_id: int) -> list[Season]:
    """The tournament's seasons, newest first."""
    async with client(transport) as api:
        response = await api.get(f"/unique-tournament/{tournament_id}/seasons")
    raise_for_failure(response)
    if response.status_code == 404:
        return []
    return [Season(season["id"], season["year"]) for season in response.json()["seasons"]]


async def fetch_finished_event_ids(
    transport: httpx.AsyncBaseTransport, tournament_id: int, season_id: int
) -> list[int]:
    """Every finished Match of a season, newest first, each once.

    Pages of 30 events run until `hasNextPage` is false. A page past the end answers 404, as
    does the first page of a season with no Match played yet: both end the list. Postponed and
    cancelled events are left out; a rescheduled fixture can be listed twice.
    """
    event_ids: list[int] = []
    seen: set[int] = set()
    async with client(transport) as api:
        page = 0
        while True:
            response = await api.get(
                f"/unique-tournament/{tournament_id}/season/{season_id}/events/last/{page}"
            )
            raise_for_failure(response)
            if response.status_code == 404:
                break
            body = response.json()
            for event in body["events"]:
                if event["status"]["type"] == "finished" and event["id"] not in seen:
                    seen.add(event["id"])
                    event_ids.append(event["id"])
            if not body.get("hasNextPage"):
                break
            page += 1
    return event_ids


@dataclass(frozen=True)
class Fixture:
    event_id: int
    start_timestamp: int  # kickoff, Unix seconds


async def fetch_upcoming_fixtures(
    transport: httpx.AsyncBaseTransport, tournament_id: int, season_id: int
) -> list[Fixture]:
    """The season's next Matches not started yet, soonest first: the first page of 30, about
    three rounds. A season with nothing left to play answers 404."""
    async with client(transport) as api:
        response = await api.get(
            f"/unique-tournament/{tournament_id}/season/{season_id}/events/next/0"
        )
    raise_for_failure(response)
    if response.status_code == 404:
        return []
    return [
        Fixture(event["id"], event["startTimestamp"])
        for event in response.json()["events"]
        if event["status"]["type"] == "notstarted"
    ]
