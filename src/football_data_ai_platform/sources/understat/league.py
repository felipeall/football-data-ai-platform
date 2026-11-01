"""A league season's finished Matches and each Match's data, from Understat."""

from dataclasses import dataclass

import httpx

from football_data_ai_platform.sources.understat.api import client, raise_for_failure

PREMIER_LEAGUE = "EPL"  # Understat's league name


class LeagueSeasonNotFound(Exception):
    """Understat has no data for that league and season."""


class MatchDataMissing(Exception):
    """Understat answered 404 for a Match its league data lists as played."""


@dataclass(frozen=True)
class LeagueSeason:
    league: str
    year: int  # the season's start year, as Understat names it: 2025 is 25/26
    # Played Matches (`isResult`), in fixture order.
    finished_match_ids: tuple[int, ...]


async def fetch_league_season(
    transport: httpx.AsyncBaseTransport, league: str, year: int
) -> LeagueSeason:
    """A league season's teams, players and fixtures, in one request."""
    async with client(transport) as api:
        response = await api.get(f"/getLeagueData/{league}/{year}")
    raise_for_failure(response)
    if response.status_code == 404:
        raise LeagueSeasonNotFound(f"Understat has no {league} {year}")
    dates = response.json()["dates"]
    return LeagueSeason(
        league, year, tuple(int(match["id"]) for match in dates if match["isResult"])
    )


async def fetch_match(transport: httpx.AsyncBaseTransport, match_id: int) -> None:
    """One Match's rosters and Shots, in one request.

    A 404 raises `MatchDataMissing`: only Matches the league data lists as played are asked
    for, so it is a failure, not an absence of data.
    """
    async with client(transport) as api:
        response = await api.get(f"/getMatchData/{match_id}")
    raise_for_failure(response)
    if response.status_code == 404:
        raise MatchDataMissing(f"Understat has no data for Match {match_id}")
