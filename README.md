# football-intel

> Durable football data ingestion with Temporal, and an AI scouting agent on top.

**Status:** early development — local proof of concept, milestone M2 of 7. A hosted MVP behind GitHub login follows. See the [roadmap](#roadmap).

## What it does

- **Ingests live football data** from Transfermarkt (via [`transfermarkt-api`](https://github.com/felipeall/transfermarkt-api)), Sofascore and Understat, using Temporal workflows built for unreliable sources: per-source rate limits, retries with backoff, circuit breakers, and a cache of every raw response.
- **Unifies players across sources** with an identity resolution workflow: deterministic rules, fuzzy matching, an LLM judge for ambiguous cases, and human review via Temporal signals.
- **Builds style profiles** from per-90 statistics and the on-pitch location of each player's actions, stored in pgvector for similarity search.
- **Answers scouting questions** through a provider-neutral AI agent (Claude first) exposed as an MCP server: *"left-back under 23, under €15M, similar to Grimaldo"*.
- **Measures itself** with an eval suite in CI and LLM tracing in Langfuse.

## Architecture (planned)

```
 Developer machine                                  Hosted (MVP)
┌──────────────────────────────────────┐     ┌──────────────────────────────────────┐
│ Sources ─▶ Temporal workflows        │     │ Neon Postgres (marts, pgvector)      │
│              │                       │     │   ▲ read-only                        │
│              ▼                       │     │   │                                  │
│ Postgres (raw JSONB) ─▶ dbt ─────────┼────▶│ FastAPI + Claude agent (Fly.io)      │
│                                      │     │   ▲                                  │
│ MCP server (local PoC)               │     │ Next.js chat, GitHub login (Vercel)  │
└──────────────────────────────────────┘     └──────────────────────────────────────┘
```

Ingestion stays on a residential connection, where the sources answer; the hosted app serves derived outputs only ([ADR 22](docs/adr/0022-hosting.md)).

## Tech stack

ADR numbers refer to [`docs/adr/`](docs/adr/).

| Layer | Technology | Runs on | Phase | ADR |
|---|---|---|---|---|
| Language & tooling | Python 3.13, `uv`, `ruff`, `pyright`, `pytest`, `import-linter`, `gitleaks` | local + CI | PoC | 15, 24 |
| Orchestration | Temporal: Schedules, post-match workflows, durable timers, signals | local | PoC | 5, 6 |
| Source: Transfermarkt | [`transfermarkt-api`](https://github.com/felipeall/transfermarkt-api) SDK, in-process | local | PoC | 2, 3, 28 |
| Source: Sofascore | `curl_cffi` browser impersonation, Playwright fallback | local (residential IP) | PoC | 3, 14 |
| Source: Understat | HTTP | local | PoC | 3 |
| Ingestion resilience | Per-source rate limits, retries with backoff, circuit breaker, raw response cache | local | PoC | 4 |
| Database | Postgres 17 + pgvector (raw JSONB → staging → marts) | local (Docker) | PoC | 11 |
| Transforms | dbt (Postgres adapter), run as a Temporal activity | local | PoC | 11 |
| Identity resolution | Rules → fuzzy matching → LLM judge (Claude Haiku 5.5) → human review via Temporal signal | local | PoC | 6 |
| Style profiles | Per-90 stats + 12×8 pitch grid, cosine similarity in pgvector | local | PoC | 12 |
| Agent harness | `AgentRunner` / `LLMClient` ports; `ClaudeRunner` (Anthropic SDK Tool Runner); `ScriptedRunner` test double | local + hosted | PoC | 24 |
| LLM | Anthropic API (backend configurable); Claude Sonnet 5.5 for chat, reports and eval judge; Claude Haiku 5.5 for identity resolution | API | PoC | 24, 25 |
| Agent tools | Typed tools + read-only SQL fallback on marts | local + hosted | PoC | 13 |
| MCP | MCP server over the same tools (Claude Desktop, Claude Code) | local | PoC | 8 |
| Observability | Langfuse — self-hosted locally, Langfuse Cloud for hosted traffic | local / cloud | PoC / MVP | 18, 22 |
| Evals | Deterministic checks + LLM judge in GitHub Actions, README badge | CI | MVP | 9 |
| Long-running jobs | Temporal workflow, one Scouting Report per activity | local | MVP | 24 |
| API | FastAPI | Fly.io | MVP | 22 |
| UI | Next.js chat with inline charts | Vercel | MVP | 8, 22 |
| Auth & limits | GitHub login (Auth.js), 10 queries per user per day, Anthropic console spend limit | hosted | MVP | 22 |
| Hosted database | Neon Postgres + pgvector (read-only for the app; local workers write) | cloud | MVP | 22 |
| CI/CD | GitHub Actions: lint, types, tests, import contracts, evals | GitHub | PoC → MVP | 15 |

## Roadmap

Seven milestones in order, no deadlines ([ADR 27](docs/adr/0027-milestones-without-timing.md)). Work items live in [GitHub milestones](https://github.com/felipeall/football-intel/milestones).

| Milestone | Phase | Delivery | Status |
|---|---|---|---|
| M1 | PoC | Compose stack, source interface, Transfermarkt, raw response cache, CI | ✅ |
| M2 | PoC | Sofascore, post-match workflows, three-season Match-level backfill (full pass in M7) | ✅ |
| M3 | PoC | Understat, dbt, identity resolution | 🚧 |
| M4 | PoC | Style profiles, agent tools, `ClaudeRunner` + `ScriptedRunner`, MCP server — Claude Desktop answers locally | ⏳ |
| M5 | MVP | Evals in CI, durable scouting report job | ⏳ |
| M6 | MVP | Next.js UI, FastAPI, GitHub login, quota | ⏳ |
| M7 | MVP | Deploy (Neon, Fly.io, Vercel); local workers sync to Neon | ⏳ |

**Backlog:** `LiteLLMRunner` (OpenAI, Gemini, Bedrock, Ollama, vLLM) and provider comparison evals, LiteLLM gateway, transfer rumor tracker, Opta feeds, WhoScored events, learned style embeddings, more leagues.

## Run locally

Requires Docker with Compose.

```sh
cp .env.example .env       # then replace the placeholder values
docker compose up --wait   # Postgres 17 + pgvector; with COMPOSE_PROFILES=temporal, also Temporal, migrations, worker
```

- Services are grouped in Compose profiles. Postgres always runs. `temporal` adds Temporal, the `migrate` service and the ingestion `worker`, and `.env.example` turns it on (`COMPOSE_PROFILES=temporal`). `langfuse` adds the Langfuse stack. For dbt work on stored data, Postgres alone is enough: `COMPOSE_PROFILES= docker compose up --wait`, then `uv run alembic upgrade head` if the database is new.
- Temporal gRPC on `localhost:7233`, web UI at <http://localhost:8233>.
- Postgres on `localhost:5432`; data lives in the `postgres-data` volume and survives `docker compose down`. `docker compose down -v` deletes it.
- The Temporal dev server keeps its state (schedules, running and sleeping workflows, history) in SQLite on the `temporal-data` volume, so it survives `docker compose down` and restarts. `docker compose down -v` deletes it with the other volumes. After that, recreate the post-match schedule with `uv run football-intel schedule-post-match`, which is safe to run at any time.
- To check persistence: start any workflow, run `docker compose down && docker compose up --wait`, then confirm the workflow and the `post-match-fixtures` schedule are still listed in the UI or by `temporal schedule list`.
- Langfuse runs only with its Compose profile, since nothing traces to it until the agent lands: `docker compose --profile langfuse up --wait`, or `COMPOSE_PROFILES=temporal,langfuse` in `.env`, starts it with its Postgres, ClickHouse, Redis and MinIO, and `docker compose stop langfuse-web langfuse-worker langfuse-postgres clickhouse redis minio` stops them, keeping their volumes. Name the services: `docker compose --profile langfuse stop` stops every service of the active profiles, Postgres included. Langfuse UI at <http://localhost:3000>. The first start creates the `football-intel` organisation, project and API keys, and a user from the `LANGFUSE_INIT_USER_*` variables. Check tracing with `uv run --env-file .env scripts/langfuse_smoke.py`.
- A `migrate` service applies the database migrations ([ADR 29](docs/adr/0029-migrations-alembic.md)); the `worker` service then runs the ingestion workflows.

Store one club's squad for one season as Raw Responses (club `985` is Manchester United on Transfermarkt):

```sh
uv run football-intel ingest-squad 985 --season 2025
```

Store every Premier League club's squad for a season (`--competition` takes another Transfermarkt competition ID):

```sh
uv run football-intel ingest-competition --season 2025
```

That is about 560 upstream requests, so at the default rate limit of one request per second it takes roughly ten minutes. Clubs run as child workflows, three at a time (`--concurrency`); a club that fails is listed at the end without stopping the others. Within the freshness window (24 hours by default) a re-run reuses stored Raw Responses instead of fetching again; `--refresh` fetches anyway. Rate limit, circuit breaker, freshness and proxy are set per source in `.env` (see `.env.example`).

The runs show in the Temporal UI as `squad-985-2025` and `competition-squads-GB1-2025`. Every upstream response lands in `raw.raw_responses`, untouched, before anything parses it. Scraped data stays in the database or under `data/`, never in this repository (see the [data disclaimer](#data-disclaimer)).

Store one finished Sofascore Match as Raw Responses, by Sofascore event ID (the number at the end of a match page's URL):

```sh
uv run football-intel ingest-match 16363874
```

That stores the event, its lineups with per-player statistics, the shotmap, average positions, and a heatmap and rating breakdown for every player who appeared: about 70 requests, a little over two minutes at Sofascore's default rate of one request every two seconds. Sofascore answers plain HTTP clients with 403, so its requests go through `curl_cffi` with Chrome impersonation ([ADR 14](docs/adr/0014-scraping-access.md)); rate limit, circuit breaker and proxy are set like Transfermarkt's, with `SOFASCORE__*` variables. Sofascore now challenges every request (a 403 with `"reason": "challenge"`) until a Turnstile captcha is solved and exchanged for a Captcha Token. When a request is challenged, the worker launches camoufox, an anti-detect Firefox, for a few seconds. camoufox produces the Turnstile response, and the worker exchanges it for a token that every request then carries, about once an hour ([ADR 30](docs/adr/0030-sofascore-captcha-token.md)). In the worker container the browser runs headed under Xvfb, because headless Linux fails the Turnstile. Run outside the container, it opens a window for a few seconds, or runs headless with `SOFASCORE__CAPTCHA_HEADLESS=true` on macOS. A steady one request per second drew the challenge within minutes, so Sofascore's defaults are half that rate and a 15-minute circuit-breaker cool-down. The run shows in the Temporal UI as `match-16363874`.

Backfill every finished Premier League Match of the current season and the two before it ([ADR 5](docs/adr/0005-coverage-and-refresh.md)), or one season with `--season 24/25`:

```sh
uv run football-intel backfill --match-level   # first pass: 4 requests a Match
uv run football-intel backfill --season 24/25  # full pass, a season at a time
```

The backfill runs in two passes. The Match-level pass stores the event, lineups with per-player statistics, shotmap and average positions: 4 requests a Match, about 3,300 for three seasons, under two hours. The full pass adds each appearing player's heatmap and rating breakdown, 64 of a Match's ~68 requests and about 56,000 over three seasons (around 31 hours at Sofascore's default rate). Run it slowly, a season at a time; it reuses the Match-level responses still inside the Freshness Window and only needs the per-player ones. Post-match workflows always ingest new Matches in full. A bot challenge is answered with a Captcha Token ([ADR 30](docs/adr/0030-sofascore-captcha-token.md)). Only when no token can be obtained does a challenged Match fail without retries; the season then starts no further Matches and the backfill no further seasons. The run lists the Matches it did not attempt, and a re-run picks them up. Each season runs as a child workflow, and each Match as its own `MatchIngestWorkflow`, three at a time (`--concurrency`), all sharing Sofascore's rate limit. A Match that fails is listed at the end without stopping the others. The run is durable: it survives a worker restart. Re-running it resumes, because Matches already stored completely (every Raw Response a replay needs, none of them a failure) are skipped without a request. Finished Matches do not change, so completeness decides, not the Freshness Window; `--refresh` re-fetches everything anyway. The run shows in the Temporal UI as `backfill-17`.

New Matches can be ingested automatically after they are played, by a daily Schedule. While the stack runs locally, ingestion runs by hand, so the Schedule commands create their Schedules **paused**, and running one again pauses a running Schedule. Create, or update, the Schedule:

```sh
uv run football-intel schedule-post-match
```

Every day at 06:00 UTC it reads the Premier League's next fixtures and starts one post-match workflow per Match kicking off within eight days, named `post-match-<event id>`; a fixture seen on several days starts only one. Each one sleeps on a durable timer until kickoff + 4 hours (final whistle + 2 hours, [ADR 5](docs/adr/0005-coverage-and-refresh.md)), then checks the Match. Finished, it is ingested like `ingest-match`. Moved to a new kickoff, the wait starts over. Still on, it is checked again every 30 minutes, up to 12 times. Postponed or cancelled, the workflow ends, and a later run picks up the new date. Start it with `temporal schedule toggle --schedule-id post-match-fixtures --unpause --reason "<why>"`, or run it once while paused with `temporal schedule trigger --schedule-id post-match-fixtures`. Pausing the Schedule does not stop post-match workflows already started: each still ingests its Match after its timer.

Understat is the second, independent source of xG ([ADR 3](docs/adr/0003-live-data-sources.md)). Store its last three Premier League seasons, or one season by its start year (`2025` is 25/26):

```sh
uv run football-intel understat-backfill            # current season and the two before it
uv run football-intel understat-backfill --season 2025
uv run football-intel schedule-understat            # daily at 07:00 UTC, current season; created paused
```

Each season is one request for the league data (teams, players and fixtures, always fetched fresh) plus one per played Match (rosters and Shots), about 1 request per second (`UNDERSTAT__REQUESTS_PER_SECOND`). A Match already stored is not requested again. A Match the league data lists as played whose data answers 404 is listed as failed, and the next run tries it again. The daily Schedule runs the same workflow for the current season only, so it picks up the previous day's Matches. Understat answers every data request without the `X-Requested-With: XMLHttpRequest` header with a 404, the same as for a Match that does not exist; the client always sends it.

Every upstream request also leaves one Fetch Attempt in `raw.fetch_attempts`, including timeouts, transport errors, circuit-breaker rejections and reuses of a fresh Raw Response. Print each source's outcomes and success rate per day (UTC) for the last seven days, or for a range with `--from` and `--to`:

```sh
uv run football-intel success-rates --from 2026-10-01 --to 2026-10-13
```

The success rate shows breakage: successful answers and 404s (the source answered; the thing does not exist) over the attempts that reached the source. Circuit-breaker rejections and reuses never reached it, so they are counted but left out of the rate. An answer counts as blocked when it is a 403 or 429, or when the source's own failure test rejects it, such as Transfermarkt's WAF challenge served with a 200. The same numbers are in the `raw.source_success_rates` view.

Transforms run in dbt ([ADR 11](docs/adr/0011-transform-layer.md)), from the project in `dbt/`. It reads `raw.raw_responses` and builds views in the `staging` schema and tables in `marts`. Build and test everything:

```sh
uv run --env-file .env dbt build --project-dir dbt --profiles-dir dbt
```

dbt reads the connection from the `POSTGRES_*` variables, so `--env-file .env` passes them. Staging uses the latest successful Raw Response per URL, so re-fetches and failed answers drop out. `staging.stg_sofascore__matches` has the finished Sofascore Matches, `staging.stg_sofascore__appearances` one row per player with minutes in a Match, and `marts.player_season_minutes` the Appearances and minutes per player and season. From Understat, `staging.stg_understat__matches` has the played Matches with their xG, `staging.stg_understat__appearances` one row per player with minutes, and `staging.stg_understat__shots` every Shot with its location and xG. `marts.clubs` maps each Club to its Sofascore, Understat and Transfermarkt IDs, from the hand-maintained seed `dbt/seeds/club_source_ids.csv`; a club new to the league needs a row there, or `dbt build` fails its tests. `marts.matches` links each source's Match to one canonical Match by Season, home Club and away Club (`2025:arsenal:chelsea`), with every source's ID. A test checks that linked Matches agree on the score, and a warning lists Matches only one source has stored yet.

Identity Resolution links the staged Source Records to canonical Players ([ADR 6](docs/adr/0006-identity-resolution.md), [ADR 31](docs/adr/0031-identity-schema-and-transform-runs.md), [ADR 32](docs/adr/0032-reep-first-mapping-layer.md)). It takes the curated mappings of the [Reep](https://reep.football) register first, from a pinned release, and its own rules decide only where Reep has none. Pin the release in `.env` (`REEP__RELEASE=2026-10-25.1`), load it once, then run Identity Resolution after `dbt build`:

```sh
uv run football-intel reep-status          # pinned, loaded and current releases, and what changed
uv run football-intel load-reep            # download, verify and load the pinned release
uv run football-intel resolve-identities
```

- **Checking for a new release.** Reep publishes data releases on `data.reep.football`, often more than once a week. Nothing announces them, and the GitHub repository's tags are code versions, not data. `reep-status` reads `latest.json` and each newer release's manifest, which describes the changes from its predecessor: new bridges per provider, the change in players and teams, ID retention, and the release's own quality gate. It also counts the persons, teams and bridges of each loaded release. It downloads no bridges.
- **Loading a release.** `load-reep` fetches the pinned release's manifest, `bridges.parquet` (about 376 MB, every entity type) and `redirects.parquet` into `data/reep/<release>/`, and checks each file's SHA-256 against the manifest. A mismatch stops it, and a file already verified is not downloaded again. It then keeps the person and team bridges of every entity Reep maps to at least two of Sofascore, Transfermarkt and Understat (about 29k persons and 9k teams; Transfermarkt teams by their `verein` ID), with merged Reep IDs resolved, and loads them into `reference.reep_player_bridges` and `reference.reep_team_bridges`, replacing that release's rows ([ADR 34](docs/adr/0034-canonical-ids-are-ours.md)). The manifest and its gate result go to `reference.reep_releases`.
- **Upgrading.** Set `REEP__RELEASE` to the new stamp, then run `load-reep` and `resolve-identities`. A new release only links Source Records that have no Candidate yet, so links and Player IDs never change. `resolve-identities` stops if the pinned release is not loaded.

How a Source Record is linked:

1. **Reep.** A Reep bridge between a Transfermarkt or Understat player we hold and a Sofascore player we hold is an accepted Candidate (`reep_bridge`, decided by `reep:<release>`).
2. **Rules, for the rest.** Every Sofascore player is a Player. A Transfermarkt player links to the Sofascore player with the same birth date and the same name. An Understat player links to the Sofascore teammate who played the same Matches for the same Club with the same name and minutes.
3. **Fuzzy scoring.** Other close candidates are scored on name, co-appearance and minutes. A clear best links as `fuzzy`; the rest stay pending for the LLM judge.

Results go to the Alembic-managed `identity` schema: `identity.players`, and `identity.identity_candidates` with each Candidate's method, confidence, status and evidence. A re-run proposes only Source Records that have no Candidate yet. The one exception is a rejected Reep link: setting a `reep_bridge` Candidate's status to `rejected` lets the rules link its Source Record on the next run. Transfermarkt has only the current squads, so players who left the Premier League since 24/25 have no Transfermarkt link.

## Development

Requires [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync                    # Python 3.13 and dev tools
uv run pytest              # tests; database tests need the compose Postgres
uv run ruff check && uv run ruff format --check
uv run pyright
uv run lint-imports        # provider SDKs only inside agent/providers/<provider>/ (ADR 24)
```

## Documentation

- [`GLOSSARY.md`](GLOSSARY.md) — domain glossary
- [`docs/adr/`](docs/adr/) — architecture decision records
- [`AGENTS.md`](AGENTS.md) — rules for coding agents: commits, branches, pull requests, critical review
- [`docs/agent-skills.md`](docs/agent-skills.md) — official agent skills enabled for coding agents

## Data disclaimer

This project collects data from third-party websites for personal, non-commercial research. No scraped data is stored in or distributed through this repository. The same goes for the Reep register's releases: they are CC0 but derived from providers' data, so they stay under `data/` and in the database. If you run it, you are responsible for complying with each source's terms of service.

## Prior work

This project supersedes [`football-data-platform`](https://github.com/felipeall/football-data-platform) (v1).

## License

[MIT](LICENSE)
