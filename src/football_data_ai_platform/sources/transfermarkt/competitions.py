"""Competitions from Transfermarkt."""

import httpx
from transfermarkt_api import Transfermarkt
from transfermarkt_api.schemas import CompetitionClubs

# Transfermarkt's competition ID for the Premier League, the only league in scope (ADR 5).
PREMIER_LEAGUE = "GB1"


async def fetch_competition_clubs(
    transport: httpx.AsyncBaseTransport, competition_id: str, season_id: str | None
) -> CompetitionClubs:
    """The clubs in a competition season. Every upstream request goes through `transport`."""
    async with Transfermarkt(transport=transport, cache=None) as tm:
        return await tm.competition_clubs(competition_id, season_id=season_id)
