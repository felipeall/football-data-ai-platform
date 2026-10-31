# 35. The PoC covers last season only; Transfermarkt squads for that season

Status: Accepted — 2026-10-31. Amends ADR 5 (coverage) and ADR 34 (Transfermarkt squads for every season we cover).

## Context
ADR 5 covers the current season and the two before it. ADR 34 has Transfermarkt create the Players that Reep lacks, from squads ingested for every season we cover. It assumes that a past season's squad "lists every player registered with a club in that season".

A check on 2026-10-31 (#68) found two problems.

- **The stored squads were already a past season.** The 20 squads stored on 2026-10-12 are `season=2025` (25/26), and every entry has `"type": "historical"`. They are not today's squads, as ADR 34 and the README said.
- **A past season's squad is the roster at the end of that season.** It is not every player registered during the season. Of 407 times a Reep-bridged player played for a club in 25/26, 25 are missing from that club's 2025 squad, and 17 are missing from every Premier League 2025 squad. The missing players are those who left during the season, in late August or in January (Eze, Guéhi, Agbadou, Strand Larsen), and academy debutants who are not on the first-team roster.

The current season (26/27) is still being played. Its squads change with every signing and debutant, and covering it means refreshing them by hand while schedules are off.

## Decision
- **The PoC covers one season: 25/26,** the last finished Premier League season. Neither the current season nor earlier seasons are covered.
- **Transfermarkt squads are ingested for that season only** (`ingest-competition --season 2025`). A finished season's squads do not change, so they need no refresh.
- **ADR 34's claim is corrected.** A past season's squad lists the players at the club when the season ended. A player who left during the season, or debuted from the academy, may have no Transfermarkt record. As ADR 34's Consequences already allow, such a player gets no Player, and each Transform Run lists them.
- **Data already stored for 26/27 stays in `raw`.** These are Sofascore and Understat Matches. It is not deleted, and it is not ingested further while the PoC runs.

## Consequences
- **The gap is measured on 25/26.**
  - 141 of the players who played in 25/26 have no Reep person. Transfermarkt has 114 of them, and **27 have neither**. 22 are academy players born in 2004 or later. The other 5 are senior players missing from their club's end-of-season squad: Aguerd, Dan Neil, Cirkin, Riad and Seelt.
  - They wait for a newer Reep release, or for a later decision that adds a Transfermarkt source listing every player who appeared, such as per-match lineups.
- **ADR 34's baseline of 874, 604 and 270 cannot be reproduced** from the data stored today. Counts from now on use this definition: distinct Sofascore players with an Appearance in a stored 25/26 Match.
- **Trend features across seasons (ADR 5) wait** until a second season is covered. Covering more seasons later means ingesting their squads and Matches, and adding a season costs about 560 Transfermarkt requests.
- **The backfill and Understat commands keep their three-season defaults.** Run them with `--season` for 25/26. Restricting the defaults, and keeping 26/27 Matches out of the marts, is later work.
