# 31. Identity tables in Alembic; dbt in serialized Transform Runs

Status: Accepted — 2026-10-25; step order refined the same day. Amended by ADR 34 (four Transform Run steps). Amends ADR 11 (transform layer) and ADR 29 (migrations).

## Context

### Identity tables
Identity Resolution (ADR 6) links Source Records to Players. Its output holds state that cannot be derived again from Raw Responses:

- human approvals and rejections, delivered as Temporal signals;
- LLM judge verdicts, which cost money and are not reproducible run to run;
- Player IDs, which the agent cites in answers and Scouting Reports, so they must stay the same across rebuilds.

dbt rebuilds its relations from their sources on every run, so it cannot own this state. ADR 29 limits Alembic migrations to "the tables that ingestion writes", in the `raw` schema, so nothing owns it today.

### Transform cadence
ADR 11 runs dbt as an activity after each Match ingest. Two things make that a poor fit:

- **Concurrency.** The backfill ingests three Matches at once (`--concurrency`, default 3), and post-match workflows finish whenever their Matches do. Per-Match runs would start concurrent `dbt build`s, which on Postgres replace the same relations and fail or block on each other's locks.
- **Volume.** A three-season backfill would start about 1,140 dbt runs. Each rebuilds every model, while one run after the backfill gives the same result.

Identity Resolution also sits between dbt's layers. It reads Source Records from staging, and the canonical Clubs and Matches, and the models that read its links come after it. So a transform is: what does not read `identity`, then identity, then what does.

## Decision

- **An `identity` schema, managed by Alembic.** It holds Players, and Identity Candidates with their method, confidence and status (pending, accepted or rejected). An accepted Candidate is the link from a Source Record to a Player; partial unique indexes keep one link per Source Record and one per Player and source. Migrations follow ADR 29 (plain SQL, `migrations/versions/`). Only the identity resolution workflow and the review signal write to it.
- **dbt reads `identity` as a source and never writes to it.** dbt keeps owning staging and marts (ADR 11).
- **A Transform Run is one `TransformWorkflow`.** It runs:
  1. `dbt build --exclude source:identity+`: everything that does not read the `identity` schema (staging, Clubs, Matches);
  2. the deterministic and scoring steps of identity resolution, which write new Players, links and Identity Candidates;
  3. `dbt build --select source:identity+`: the models that read it.

  Each step is an activity. dbt runs as a subprocess of the activity, so it can come from its own environment if its dependencies conflict with the worker's.
- **Only one Transform Run at a time.** The workflow has a fixed ID, `transform`, and is started only through a Schedule with overlap policy `SKIP` or a CLI command. A start while a run is going joins that run instead of starting a second one.
- **When it runs:**
  - daily, from a Schedule, after the night's post-match ingests;
  - on demand, from `fdap transform`, for example after a backfill.

  Ingest workflows no longer start dbt.
- **Human review stays outside the run.** The LLM judge and review signals (ADR 6) act on Identity Candidates in the `identity` schema whenever they arrive. The next Transform Run carries their decisions into the marts.

## Consequences

- Marts lag ingestion by up to a day, unless `fdap transform` is run. No consumer needs fresher data: the agent answers scouting questions, not live ones.
- Accepted links and Player IDs survive any dbt rebuild, and a full refresh of the marts is always safe.
- The `identity` schema is backed up and migrated like `raw`. It is the only place human review decisions live.
- Tests:
  - identity migrations run in the existing test fixtures;
  - the workflow's order and its single-run rule are workflow tests with stubbed activities.
- **Amendments:**
  - ADR 11: dbt runs in Transform Runs, not after each Match ingest.
  - ADR 29: Alembic also manages the `identity` schema, which the identity resolution workflow writes.
