# Running locally

How to start the stack, run each ingestion and the Transform Run, and run the development tools. For what the pipeline does and why, see the [README](../README.md); for the Temporal workflows behind these commands, see [`temporal.md`](temporal.md).

## Start the stack

Requires Docker with Compose.

```sh
cp .env.example .env       # then replace the placeholder values
docker compose up --wait   # Postgres 17 + pgvector; with COMPOSE_PROFILES=temporal, also Temporal, migrations, worker
```

- Services are grouped in Compose profiles. Postgres always runs. `temporal` adds Temporal, the `migrate` service and the ingestion `worker`, and `.env.example` turns it on (`COMPOSE_PROFILES=temporal`). `langfuse` adds the Langfuse stack. For dbt work on stored data, Postgres alone is enough: `COMPOSE_PROFILES= docker compose up --wait`, then `uv run alembic upgrade head` if the database is new.
- Temporal gRPC on `localhost:7233`, web UI at <http://localhost:8233>.
- Postgres on `localhost:5432`; data lives in the `postgres-data` volume and survives `docker compose down`. `docker compose down -v` deletes it.
- The Temporal dev server keeps its state (schedules, running and sleeping workflows, history) in SQLite on the `temporal-data` volume, so it survives `docker compose down` and restarts. `docker compose down -v` deletes it with the other volumes. After that, recreate the post-match schedule with `uv run fdap schedule-post-match`, which is safe to run at any time.
- To check persistence: start any workflow, run `docker compose down && docker compose up --wait`, then confirm the workflow and the `post-match-fixtures` schedule are still listed in the UI or by `temporal schedule list`.
- Langfuse runs only with its Compose profile, since nothing traces to it until the agent lands: `docker compose --profile langfuse up --wait`, or `COMPOSE_PROFILES=temporal,langfuse` in `.env`, starts it with its Postgres, ClickHouse, Redis and MinIO, and `docker compose stop langfuse-web langfuse-worker langfuse-postgres clickhouse redis minio` stops them, keeping their volumes. Name the services: `docker compose --profile langfuse stop` stops every service of the active profiles, Postgres included. Langfuse UI at <http://localhost:3000>. It runs v4 in events-only mode: read traces through the v2 Observations API (`/api/public/v2/observations`), since the legacy `/api/public/traces` endpoints answer with an error. The first start creates the `football-data-ai-platform` organisation, project and API keys, and a user from the `LANGFUSE_INIT_USER_*` variables. Check tracing with `uv run --env-file .env scripts/langfuse_smoke.py`.
- A `migrate` service applies the database migrations ([ADR 29](adr/0029-migrations-alembic.md)); the `worker` service then runs the ingestion workflows.

Store one club's squad for one season as Raw Responses (club `985` is Manchester United on Transfermarkt):

```sh
uv run fdap ingest-squad 985 --season 2025
```

Store every Premier League club's squad for a season (`--competition` takes another Transfermarkt competition ID):

```sh
uv run fdap ingest-competition --season 2025
```

That is about 560 upstream requests, so at the default rate limit of one request per second it takes roughly ten minutes. Clubs run as child workflows, three at a time (`--concurrency`); a club that fails is listed at the end without stopping the others. Within the freshness window (24 hours by default) a re-run reuses stored Raw Responses instead of fetching again; `--refresh` fetches anyway. Rate limit, circuit breaker, freshness and proxy are set per source in `.env` (see `.env.example`).

The runs show in the Temporal UI as `squad-985-2025` and `competition-squads-GB1-2025`. Every upstream response lands in `raw.raw_responses`, untouched, before anything parses it. Scraped data stays in the database or under `data/`, never in this repository (see the [data disclaimer](../README.md#data-disclaimer)).

Store one finished Sofascore Match as Raw Responses, by Sofascore event ID (the number at the end of a match page's URL):

```sh
uv run fdap ingest-match 16363874
```

That stores the event, its lineups with per-player statistics, the shotmap, average positions, and a heatmap and rating breakdown for every player who appeared: about 70 requests, a little over two minutes at Sofascore's default rate of one request every two seconds. Sofascore answers plain HTTP clients with 403, so its requests go through `curl_cffi` with Chrome impersonation ([ADR 14](adr/0014-scraping-access.md)); rate limit, circuit breaker and proxy are set like Transfermarkt's, with `SOFASCORE__*` variables. Sofascore now challenges every request (a 403 with `"reason": "challenge"`) until a Turnstile captcha is solved and exchanged for a Captcha Token. When a request is challenged, the worker launches camoufox, an anti-detect Firefox, for a few seconds. camoufox produces the Turnstile response, and the worker exchanges it for a token that every request then carries, about once an hour ([ADR 30](adr/0030-sofascore-captcha-token.md)). In the worker container the browser runs headed under Xvfb, because headless Linux fails the Turnstile. Run outside the container, it opens a window for a few seconds, or runs headless with `SOFASCORE__CAPTCHA_HEADLESS=true` on macOS. A steady one request per second drew the challenge within minutes, so Sofascore's defaults are half that rate and a 15-minute circuit-breaker cool-down. The run shows in the Temporal UI as `match-16363874`.

Backfill every finished Premier League Match of the current season and the two before it ([ADR 5](adr/0005-coverage-and-refresh.md)), or one season with `--season 24/25`:

```sh
uv run fdap backfill --match-level   # first pass: 4 requests a Match
uv run fdap backfill --season 24/25  # full pass, a season at a time
```

The backfill runs in two passes. The Match-level pass stores the event, lineups with per-player statistics, shotmap and average positions: 4 requests a Match, about 3,300 for three seasons, under two hours. The full pass adds each appearing player's heatmap and rating breakdown, 64 of a Match's ~68 requests and about 56,000 over three seasons (around 31 hours at Sofascore's default rate). Run it slowly, a season at a time; it reuses the Match-level responses still inside the Freshness Window and only needs the per-player ones. Post-match workflows always ingest new Matches in full. A bot challenge is answered with a Captcha Token ([ADR 30](adr/0030-sofascore-captcha-token.md)). Only when no token can be obtained does a challenged Match fail without retries; the season then starts no further Matches and the backfill no further seasons. The run lists the Matches it did not attempt, and a re-run picks them up. Each season runs as a child workflow, and each Match as its own `MatchIngestWorkflow`, three at a time (`--concurrency`), all sharing Sofascore's rate limit. A Match that fails is listed at the end without stopping the others. The run is durable: it survives a worker restart. Re-running it resumes, because Matches already stored completely (every Raw Response a replay needs, none of them a failure) are skipped without a request. Finished Matches do not change, so completeness decides, not the Freshness Window; `--refresh` re-fetches everything anyway. The run shows in the Temporal UI as `backfill-17`.

New Matches can be ingested automatically after they are played, by a daily Schedule. While the stack runs locally, ingestion runs by hand, so the Schedule commands create their Schedules **paused**, and running one again pauses a running Schedule. Create, or update, the Schedule:

```sh
uv run fdap schedule-post-match
```

Every day at 06:00 UTC it reads the Premier League's next fixtures and starts one post-match workflow per Match kicking off within eight days, named `post-match-<event id>`; a fixture seen on several days starts only one. Each one sleeps on a durable timer until kickoff + 4 hours (final whistle + 2 hours, [ADR 5](adr/0005-coverage-and-refresh.md)), then checks the Match. Finished, it is ingested like `ingest-match`. Moved to a new kickoff, the wait starts over. Still on, it is checked again every 30 minutes, up to 12 times. Postponed or cancelled, the workflow ends, and a later run picks up the new date. Start it with `temporal schedule toggle --schedule-id post-match-fixtures --unpause --reason "<why>"`, or run it once while paused with `temporal schedule trigger --schedule-id post-match-fixtures`. Pausing the Schedule does not stop post-match workflows already started: each still ingests its Match after its timer.

Understat is the second, independent source of xG ([ADR 3](adr/0003-live-data-sources.md)). Store its last three Premier League seasons, or one season by its start year (`2025` is 25/26):

```sh
uv run fdap understat-backfill            # current season and the two before it
uv run fdap understat-backfill --season 2025
uv run fdap schedule-understat            # daily at 07:00 UTC, current season; created paused
```

Each season is one request for the league data (teams, players and fixtures, always fetched fresh) plus one per played Match (rosters and Shots), about 1 request per second (`UNDERSTAT__REQUESTS_PER_SECOND`). A Match already stored is not requested again. A Match the league data lists as played whose data answers 404 is listed as failed, and the next run tries it again. The daily Schedule runs the same workflow for the current season only, so it picks up the previous day's Matches. Understat answers every data request without the `X-Requested-With: XMLHttpRequest` header with a 404, the same as for a Match that does not exist; the client always sends it.

Every upstream request also leaves one Fetch Attempt in `raw.fetch_attempts`, including timeouts, transport errors, circuit-breaker rejections and reuses of a fresh Raw Response. Print each source's outcomes and success rate per day (UTC) for the last seven days, or for a range with `--from` and `--to`:

```sh
uv run fdap success-rates --from 2026-10-01 --to 2026-10-13
```

The success rate shows breakage: successful answers and 404s (the source answered; the thing does not exist) over the attempts that reached the source. Circuit-breaker rejections and reuses never reached it, so they are counted but left out of the rate. An answer counts as blocked when it is a 403 or 429, or when the source's own failure test rejects it, such as Transfermarkt's WAF challenge served with a 200. The same numbers are in the `raw.source_success_rates` view.

Transforms run in dbt ([ADR 11](adr/0011-transform-layer.md)), from the project in `dbt/`. It reads `raw.raw_responses` and the `identity` schema, and builds views in the `staging` schema and tables in `marts`. One Transform Run ([ADR 31](adr/0031-identity-schema-and-transform-runs.md), [ADR 34](adr/0034-canonical-ids-are-ours.md)) builds everything, after `uv run alembic upgrade head` and `load-reep` (below):

```sh
uv run fdap transform
```

It starts the `TransformWorkflow`, whose fixed ID is `transform`, so a start while a run is going joins that run. The worker runs its four steps, each an activity:

1. dbt builds staging: `--select staging --exclude source:identity+`;
2. the Club step creates and links Clubs (below);
3. dbt builds `clubs` and `matches`, those two models only;
4. Identity Resolution creates Players and links Source Records to them (below), then dbt builds the rest: `--exclude staging clubs matches`.

dbt runs as a subprocess of the worker, with `--indirect-selection cautious`, so a test runs in the step that builds the last of its parents. A failed model or test fails the run without retries; a build that lost its database connection is retried. A team or player that links to nothing never fails it: the command prints each step's time, dbt's warnings, the Clubs created, the teams with no Club, and the players that link to no Player. No Schedule starts it while the stack is local; run it after ingesting.

To run dbt by hand, for example one model while working on it: `uv run --env-file .env dbt build --project-dir dbt --profiles-dir dbt --select <model>`. dbt reads the connection from the `POSTGRES_*` variables, so `--env-file .env` passes them. Staging uses the latest successful Raw Response per URL, so re-fetches and failed answers drop out. `staging.stg_sofascore__matches` has the finished Sofascore Matches with their round, `staging.stg_sofascore__appearances` one row per player with minutes in a Match, `staging.stg_sofascore__actions` the located Actions from each player's rating breakdown (a 404 is no Actions), and `staging.stg_sofascore__shots` the shotmap's Shots with xG and xGOT. From Understat, `staging.stg_understat__matches` has the played Matches with their xG, `staging.stg_understat__appearances` one row per player with minutes, and `staging.stg_understat__shots` every Shot with its location, xG and assister. From Transfermarkt, `staging.stg_transfermarkt__players` has the squads' players with their current market value, and `staging.stg_transfermarkt__clubs` every club the batched `/clubs` responses name, with its country.

Clubs, Competitions and Stages have our own IDs, kept in `identity` ([ADR 34](adr/0034-canonical-ids-are-ours.md)):

- **Competitions and Stages** are entered by hand, in a migration, when we take one on. The Premier League is `premier-league` (Sofascore 17, Understat `EPL`, Transfermarkt `GB1`), with one Stage, `league`. A Stage link can map a range of Sofascore round numbers to another Stage. Ranges never overlap, and apply to every season: a cup that renumbers its rounds by season would need a season on the link.
- **The Club step** gives a Club to every team in the stored Matches of those Competitions. The pinned Reep release's team bridges link the team, and the Reep team creates the Club the first time we see it. The Club ID is a slug of its Transfermarkt name: ASCII, `&` as `and`, apostrophes and dots dropped, the words `fc`, `afc` and `cf` dropped (`AFC Bournemouth` is `bournemouth`, `Brighton & Hove Albion` is `brighton-and-hove-albion`). A slug already taken gets the country appended (`-england`). A re-run creates nothing for teams already linked, and never moves a link. A team without a Reep team, or whose Reep team has no Transfermarkt name, gets no Club: `transform` lists it and why.
- **`rename-club <club> <new-slug>`** renames a Club. The former slug stays as an alias, and `marts.match_id_aliases` resolves the Match IDs built from it.

`marts.clubs` has each Club with its Sofascore, Understat, Transfermarkt and Reep IDs. `marts.matches` links each source's Match to one canonical Match by Competition, season, home Club and away Club, then takes the Stage from the Sofascore round's Stage link, else the default: `premier-league:2025:league:arsenal:west-ham-united`. A Match whose Competition or a team has no link is left out, and a warning lists it; it never fails the build. Tests check that linked Matches agree on the score and that no fixture is two Matches with home and away swapped. A warning lists Matches only one source has stored yet.

The Player marts are keyed to the canonical Player and Match, and hold the covered Season only ([ADR 35](adr/0035-poc-covers-last-season.md)). The Transform Run passes `COVERED_SEASON` to dbt as the var `covered_seasons`. When you run dbt by hand, the var defaults to `[2025]`; override it with `--vars '{covered_seasons: [2024]}'`.

- `marts.players` has each Player linked to a Source Record, with Transfermarkt's attributes where linked (birth date, position, current market value), else Sofascore's, and `understat_position`, the detailed position (DL, AMC…) of the Understat Appearances they started. `nationality_code` is the Player's Nationality (`CI`, `GB-ENG`) whichever source gave it, and `nationality` our name for it. A country that maps to no code is listed by the warning `warn_players_with_an_unmapped_nationality`: add it to `dbt/seeds/country_mappings.csv`.
- `marts.appearances` has one row per Player and Match, with minutes and Sofascore's per-match statistics (0 where Sofascore leaves a zero out) next to Understat's xG and xA.
- `marts.actions` has the located Actions from the rating breakdowns.
- `marts.shots` has both sources' Shots, one row per source Shot, with own goals left out.
- `marts.player_season_minutes` has the Appearances and minutes per Player and Season.

Actions and Shots share one frame: x and y run 0–100, x from the team's own goal line towards the goal attacked, and y from the right touchline to the left one. A Source Record linked to no Player stays out of these marts, and the warning `warn_appearances_left_out_of_the_player_marts` lists each one with its minutes.

Identity Resolution creates Players and links the staged Source Records to them ([ADR 6](adr/0006-identity-resolution.md), [ADR 31](adr/0031-identity-schema-and-transform-runs.md), [ADR 32](adr/0032-reep-first-mapping-layer.md), [ADR 34](adr/0034-canonical-ids-are-ours.md)). The [Reep](https://reep.football) register, from a pinned release, and Transfermarkt create Players; Sofascore and Understat only link to them. Pin the release in `.env` (`REEP__RELEASE=2026-10-31.1`) and load it once. Identity Resolution then runs in each Transform Run:

```sh
uv run fdap reep-status          # pinned, loaded and current releases, and what changed
uv run fdap load-reep            # download, verify and load the pinned release
uv run fdap transform
```

- **Checking for a new release.** Reep publishes data releases on `data.reep.football`, often more than once a week. Nothing announces them, and the GitHub repository's tags are code versions, not data. `reep-status` reads `releases/latest.json`, then each newer release's manifest, found through its file list (`releases/<release>/release.json`). A manifest describes the changes from its predecessor: new bridges per provider, the change in players and teams, ID retention, and the release's own quality gate. It also counts the persons, teams and bridges of each loaded release, and prints them before it calls Reep, so they show even when Reep can't be read (the command then exits with an error). It downloads no bridges.
- **Loading a release.** `load-reep` reads the pinned release's file list (`releases/<release>/release.json`). It fetches the manifest, `bridges.parquet` (about 395 MB, every entity type), `redirects.parquet` and `reep-register.duckdb` (about 1.2 GB, the whole register for the football-docs MCP server) from the keys listed there into `data/reep/<release>/`, and checks each file's SHA-256 against both the file list and the manifest. A mismatch stops it, and a file already verified is not downloaded again. It then keeps the person and team bridges of every entity Reep maps to at least two of Sofascore, Transfermarkt and Understat (about 29k persons and 9k teams; Transfermarkt teams by their `verein` ID), with merged Reep IDs resolved, and loads them into `reference.reep_player_bridges` and `reference.reep_team_bridges`, replacing that release's rows ([ADR 34](adr/0034-canonical-ids-are-ours.md)). The manifest and its gate result go to `reference.reep_releases`.
- **Reep lookups for coding agents.** `load-reep` points `data/reep/reep-register.duckdb` at the pinned release's DuckDB register. `.mcp.json` sets `REEP_DUCKDB_PATH` to that link, relative to the checkout (a worktree's `data/` links to the main checkout's), so the football-docs MCP server's `resolve_entity` tool answers from it ([agent skills](agent-skills.md)).
- **Upgrading.** Set `REEP__RELEASE` to the new stamp, then run `load-reep` and `transform`. A new release only links Source Records and teams that have no link yet, so links, Club IDs and Player IDs never change. `transform` stops at the Club step if the pinned release is not loaded.

How Players are created and Source Records linked, in order:

1. **Reep.** Every Source Record we hold that the release maps to a Reep person links to that person's Player (`reep_bridge`, decided by `reep:<release>`). The first record we see of a person creates the Player; later ones, from any source, link to it. A link our own data contradicts waits for the judge instead: a Transfermarkt birth date more than a day off the Player's Sofascore record, or an Understat player who never played in the same Match for the same Club as it. The Candidate's evidence says why.
2. **Transfermarkt.** Each Transfermarkt player Reep did not link creates a Player (`transfermarkt_player`).

   Before step 1 or 2 creates a Player, the creation check runs the Sofascore rule below in reverse ([ADR 39](adr/0039-creation-check.md)). A Transfermarkt record is checked against Players with a Sofascore link and no Transfermarkt link. A Reep person's Sofascore record is checked against Players with a Transfermarkt link and no Sofascore link. The same name links to that Player (`creation_check`), and the person's other records follow. A similar name leaves a pending `creation_check` Candidate and creates nothing; a Reep person waits as a whole meanwhile.
3. **Sofascore,** for what Reep did not link: a birth date within a day of a Player's Transfermarkt record and the same name links (`name_birth_date`, the gap in `birth_date_gap_days`); a similar name, or a second same-named record inside that day, waits for the judge (`fuzzy`).
4. **Understat,** for what Reep did not link: the Sofascore teammate who played the same Matches for the same Club, scored on name, co-appearance and minutes. The same name and minutes link (`co_appearance`), a clear best links as `fuzzy`, and a close one waits for the judge. A player whose own Sofascore teammate has no Player is left unlinked.

Sofascore and Understat never create a Player. Each run lists the Sofascore and Understat players who played in the covered Season and link to no Player, with their minutes in it; their Appearances stay out of the Player marts until a newer Reep release or a Transfermarkt squad covers them. The covered Season is `COVERED_SEASON` in `.env`, by its start year (default `2025`, 25/26, [ADR 35](adr/0035-poc-covers-last-season.md)). Players who played only in other stored Seasons, or never played, are not listed, but they are still linked when the rules allow.

Results go to the Alembic-managed `identity` schema: `identity.players`, and `identity.identity_candidates` with each Candidate's method, confidence, status and evidence. A re-run proposes only Source Records that have no Candidate yet, so it creates no Player and changes no ID. The one exception is a rejected Reep link: setting a `reep_bridge` Candidate's status to `rejected` lets Transfermarkt or the rules link its Source Record on the next run, but never Reep again. Rejecting a `creation_check` Candidate lets its record create its own Player on the next run. Migration `0008` cleared the Players that Sofascore used to anchor, once, so Reep and Transfermarkt recreate them; it stops without clearing anything if the LLM judge or a reviewer has decided a Candidate. The PoC covers one season, 25/26, so Transfermarkt squads are stored for `--season 2025` only ([ADR 35](adr/0035-poc-covers-last-season.md)). A past season's squad is the club's roster when that season ended. A player who left during the season, or an academy player who debuted, may have no Transfermarkt record, so no Transfermarkt link.

## Development tools

Requires [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync                    # Python 3.13 and dev tools
uv run pytest              # tests; database tests need the compose Postgres
uv run ruff check && uv run ruff format --check
uv run pyright             # without a system node: uv run --with 'pyright[nodejs]' pyright
uv run lint-imports        # provider SDKs only inside agent/providers/<provider>/ (ADR 24)
```
