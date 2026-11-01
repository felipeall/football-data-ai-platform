# football-intel — Glossary

Football intelligence platform: durable ingestion of live football data (Temporal) into a unified database, with an AI scouting agent on top. A transfer rumor tracker is in the backlog.

Decisions are recorded in [`docs/adr/`](docs/adr/).

## Glossary

- **Source** — An external data provider the platform ingests from: Transfermarkt (via `transfermarkt-api`), Sofascore, Understat. Each source is implemented behind a common source interface.
- **Raw Response** — The untouched payload returned by a source for one request, stored before any parsing so it can be reprocessed without re-scraping.
- **Fetch Attempt** — One upstream request a source interface handled, and how it ended: answered (with a status code), timed out, failed in transport, rejected by the open circuit breaker, or answered from a fresh stored Raw Response. Unlike Raw Responses, Fetch Attempts include requests that got no answer; per-source success rates are computed from them.
- **Freshness Window** — How long a stored Raw Response answers its request instead of the source, set per source. Failure responses never answer; a forced refresh skips the window.
- **Challenge** — A source refusing requests until a human solves a captcha, rather than refusing one request. Sofascore's is a 403 whose body says `"reason": "challenge"`, or `"jwt:…"` when the Captcha Token it was sent is rejected. The Token Provider answers it with a new Captcha Token. Only when none can be obtained does the request fail as `ChallengeError`, which is not retried, and a backfill stops there.
- **Captcha Token** — The short-lived JWT Sofascore issues in exchange for a solved Turnstile captcha. It is bound to the IP and to the client that exchanged it, and is valid for about an hour. Sofascore's API only answers requests that carry it ([ADR 30](docs/adr/0030-sofascore-captcha-token.md)).
- **Token Provider** — The part of the Sofascore source that obtains Captcha Tokens. About once an hour, an anti-detect browser (camoufox) produces a Turnstile response, and `curl_cffi` exchanges it for the token its own requests carry ([ADR 30](docs/adr/0030-sofascore-captcha-token.md)).
- **Source Record** — A source's own representation of an entity (player, club, match), keyed by that source's ID.
- **Player** — The canonical, cross-source identity of a footballer. Its ID is ours, given once and never changed. A Player is created from the Reep Release, or from Transfermarkt where Reep has no entry; match sources only link to Players. One Player links to at most one Source Record per source ([ADR 34](docs/adr/0034-canonical-ids-are-ours.md)).
- **Identity Resolution** — The process of creating Players and Clubs and linking Source Records to them: the Reep Release's mappings first, then Transfermarkt, then deterministic rules, fuzzy scoring, an LLM judge, and human review below a confidence threshold ([ADR 34](docs/adr/0034-canonical-ids-are-ours.md)).
- **Identity Candidate** — A proposed link between a Source Record and a Player, with a confidence score, the method that produced it (the Reep Release, Transfermarkt creating the Player, a rule, fuzzy scoring, the LLM judge or a reviewer) and a status: pending, accepted or rejected. An accepted Candidate is the link. Pending ones form the ambiguous band the LLM judge and human review decide. A Reep link our own data contradicts is pending too, with the disagreement in its evidence ([ADR 32](docs/adr/0032-reep-first-mapping-layer.md)).
- **Unlinked Record** — A Sofascore or Understat Source Record that links to no Player, because neither the Reep Release nor Transfermarkt covers the player. Each Identity Resolution run lists them with their minutes; their Appearances stay out of the Player marts ([ADR 34](docs/adr/0034-canonical-ids-are-ours.md)).
- **Reep Release** — A pinned, checksum-verified release of the Reep football identity register (CC0). It maps provider IDs to one another. Identity Resolution takes its mappings first, and its own rules decide only where Reep has none ([ADR 32](docs/adr/0032-reep-first-mapping-layer.md)).
- **Transform Run** — One pass in four steps: dbt staging; Clubs created and linked; dbt Clubs and Matches; Identity Resolution for Players, then the remaining dbt models. Only one runs at a time ([ADR 31](docs/adr/0031-identity-schema-and-transform-runs.md), [ADR 34](docs/adr/0034-canonical-ids-are-ours.md)).
- **Club** — A football club, canonical across sources, created from the Reep Release or Transfermarkt. Its ID is a slug derived by a fixed rule from its Transfermarkt name (`west-ham-united`); renames keep the old slug as an alias ([ADR 34](docs/adr/0034-canonical-ids-are-ours.md)).
- **Season** — One league season, named by its start year (2025 is 2025/26, written "25/26" by Sofascore). Understat and Transfermarkt use the start year too. A calendar-year league uses its single year.
- **Competition** — A league or cup we cover, with a slug we choose (`premier-league`) and a link from each source's own competition ID.
- **Stage** — A named part of a Competition's season: `league` for a league, `league-phase`, `round-of-16` or `final` for a cup. Named by us; a Competition has a default Stage, and a stage link can map a range of a source's round numbers to one.
- **Match** — A single fixture, canonical across sources. Its ID is derived from our own IDs: Competition, Season, Stage, home Club and away Club (`premier-league:2025:league:arsenal:west-ham-united`).
- **Appearance** — One Player's participation in one Match, with minutes and per-match statistics.
- **Action** — A located on-ball event attributed to a player in a match (pass with start/end, dribble, ball carry, defensive action), from Sofascore's rating breakdown. No timestamps or sequence.
- **Shot** — A located shot with xG (and xGOT on Sofascore). Shots arrive from two independent sources: Sofascore and Understat.
- **Style Profile** — A per-player vector built from per-90 statistics and the spatial distribution of Actions over a season window, used for similarity search.
- **Scouting Query** — A natural-language request to the agent ("left-back under 23, under €15M, similar to X").
- **Scouting Report** — A generated, cited write-up about one player, produced by a long-running durable job.
- **Rumor** *(backlog)* — A structured claim extracted from news: player, from club, to club, fee, journalist, date. Resolved against actual transfers when the window closes.
- **Provider** — An LLM vendor or serving stack (Anthropic; later OpenAI-compatible endpoints such as OpenAI, Ollama, vLLM), implemented as an adapter behind the agent ports.
- **Runner** — A Provider's implementation of the `AgentRunner` port: the tool loop that answers a Scouting Query.
- **Model Role** — A named job an LLM does (`chat`, `report`, `identity_judge`, `eval_judge`). Configuration maps each role to a Provider and model ID.
