"""Club squads from Transfermarkt."""

import httpx
from transfermarkt_api import Transfermarkt
from transfermarkt_api.schemas import ClubPlayers


async def fetch_squad(
    transport: httpx.AsyncBaseTransport, club_id: str, season_id: str | None
) -> ClubPlayers:
    """Assemble a club's squad for a season. Every upstream request goes through `transport`.

    The SDK's cache is off: caching Raw Responses is ours (ADR 28).
    """
    async with Transfermarkt(transport=transport, cache=None) as tm:
        return await tm.club_players(club_id, season_id=season_id)
