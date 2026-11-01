# Worktrees and the shared stack

Several agents work at once, each in an Orca worktree under `~/orca/workspaces/football-intel/`. They share one local stack: `compose.yaml` fixes the project name `football-intel`, so every checkout talks to the same Postgres, Temporal and `worker` container.

The **main checkout** (`git rev-parse --git-common-dir`, one level up) owns that stack and the **live** database `football_intel`. The `worker` container runs `main`'s code against live, on the `ingestion` task queue. A **worktree** works against its own database and task queue, so its branch code never reaches live data or the live worker.

## Setup

`scripts/worktree.sh setup` prepares a worktree; Orca runs it as the repo's setup script on `worktree create`. Without it a worktree has no `.env`: settings fall back to defaults, and the database tests skip, reporting a pass. A `pytest` summary with skipped database tests means setup is missing.

It does four things:

- writes `.env`: a copy of the main checkout's, with `POSTGRES_DB=football_intel_wt_<slug>` and `TEMPORAL_TASK_QUEUE=wt-<slug>`, the slug taken from the worktree's folder name;
- links `data/` to the main checkout's (Reep releases and other scraped files);
- runs `uv sync`;
- creates the worktree database, empty, when Postgres is up.

`docker` is in `~/.docker/bin`, which agent shells may lack on `PATH`; the script adds it.

## Tests

`uv run pytest` builds `<worktree database>_test`, so parallel worktrees each get their own test database. The main checkout's tests use `football_intel_test`.

## Live checks

A live check runs on a copy of live, never on live itself:

1. `scripts/worktree.sh refresh-db` replaces the worktree database with a copy of live (about 300 MB, 15 s).
2. Migrate it from the worktree: `uv run alembic upgrade head`.
3. Run dbt with the worktree's settings: `uv run --env-file .env dbt build --project-dir dbt --profiles-dir dbt …`.
4. For CLI commands that start a workflow (`create-clubs`, `resolve-identities`, the ingest commands), start a worker from the worktree on its own queue, `uv run --env-file .env python -m football_intel.ingestion.worker`, then run the command from the worktree. Without that worker the workflow waits on the worktree queue, untouched.

Workflow IDs are fixed (`create-clubs`, `identity-resolution`) and share the `default` namespace with live, so a start is refused while a live run with the same ID is open. Wait for it to close (Temporal UI, <http://localhost:8233>). ADR 31's `transform` (#70) will join an open run instead.

Report live numbers from the worktree database, and name it in the issue comment.

## The live database

Live moves forward only from `main`: after a pull request merges, the main checkout runs `docker compose up --wait`, whose `migrate` service applies new migrations. Start, stop and rebuild the stack from the main checkout only.

## Work in progress

`git stash` is one stack for every worktree of the repository. Keep work in progress on your own branch, as a commit you amend or squash later.

## Teardown

`scripts/worktree.sh teardown` drops the worktree database and its test database. Orca runs it as the repo's archive script, before it removes the worktree.
