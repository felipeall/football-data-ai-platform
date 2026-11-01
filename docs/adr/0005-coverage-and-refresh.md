# 5. Coverage and refresh cadence

Status: Accepted — 2026-10-11. Amended by ADR 35: the PoC covers 25/26 only.

## Decision
- Coverage: one league (Premier League), current season plus the two previous seasons.
- Refresh: a Temporal Schedule reads upcoming fixtures and starts one workflow per match, which sleeps on a durable timer until final whistle + 2h, then ingests that match. No live in-match polling.

## Consequences
Roughly 380 matches per season with several requests each. Three seasons enable trend features (improving vs declining). Event-driven ingestion keeps request volume low.
