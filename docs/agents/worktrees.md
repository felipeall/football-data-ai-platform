# Worktrees and the shared stack

Several agents work at once, each in an Orca worktree under `~/orca/workspaces/football-data-ai-platform/`. They share one local stack: `compose.yaml` fixes the project name `football-data-ai-platform`, so every checkout talks to the same Postgres, Temporal and `worker` container.

The **main checkout** (`git rev-parse --git-common-dir`, one level up) owns that stack and the **live** database `football_data_ai_platform`. The `worker` container runs `main`'s code against live, on the `ingestion` task queue. A **worktree** works against its own database and task queue, so its branch code never reaches live data or the live worker.

## Setup

`scripts/worktree.sh setup` prepares a worktree. Orca runs it on `worktree create`, from `orca.yaml` at the repo root; `--setup skip` leaves it out, and then you run it by hand. Without it a worktree has no `.env`: settings fall back to defaults, and the database tests skip, reporting a pass. A `pytest` summary with skipped database tests means setup is missing.

It does four things:

- writes `.env`: a copy of the main checkout's, with `POSTGRES_DB=football_data_ai_platform_wt_<slug>` and `TEMPORAL_TASK_QUEUE=wt-<slug>`, the slug being the first 13 characters of the worktree's folder name plus 5 of its hash, so folders that start alike do not share a database;
- links `data/` to the main checkout's (Reep releases and other scraped files);
- runs `uv sync`;
- creates the worktree database, empty, when Postgres is up.

`docker` is in `~/.docker/bin`, which agent shells may lack on `PATH`; the script adds it.

## Tests

`uv run pytest` runs the tests in parallel with pytest-xdist, one worker per CPU, and each worker builds its own test database, `<worktree database>_test<n>` for worker `gw<n>`. Parallel worktrees, and the workers within one, never share a test database; the main checkout's are `football_data_ai_platform_test<n>`. `uv run pytest -n0` runs serially in one process, against `<worktree database>_test`, for a debugger or `-s`.

A test that creates its own database appends the `worker` fixture's number to its name, as `tests/conftest.py` and the `0008` migration test do.

## Live checks

A live check runs on a copy of live, never on live itself:

1. `scripts/worktree.sh refresh-db` replaces the worktree database with a copy of live (about 300 MB, 15 s).
2. Migrate it from the worktree: `uv run alembic upgrade head`.
3. For CLI commands that start a workflow (`transform`, the ingest commands), start a worker from the worktree on its own queue, `uv run --env-file .env python -m football_data_ai_platform.ingestion.worker`, then run the command from the worktree. Without that worker the workflow waits on the worktree queue, untouched.

Some workflow IDs are fixed and share the `default` namespace with live. `transform` is one: a worktree's `fdap transform` while a live Transform Run is open joins the live run, on live's queue and database, instead of starting its own. The command then prints `Joined Transform Run …` and warns that the run is on another queue. `identity-review` is another: a worktree's `fdap judge` or `fdap review` while live's identity review workflow is open signals the live one. Check the Temporal UI (<http://localhost:8233>) for an open `transform` or `identity-review` first, and wait for it to close. A worktree's own dbt work outside a run: `uv run --env-file .env dbt build --project-dir dbt --profiles-dir dbt …`.

Report live numbers from the worktree database, and name it in the issue comment.

## The live database

Live moves forward only from `main`: after a pull request merges, the main checkout pulls `main` and runs `docker compose up --wait --build`: `--build` rebuilds the `worker` and `migrate` images from the merged code (without it, Compose reuses the old images), and `migrate` applies new migrations. Start, stop and rebuild the stack from the main checkout only.

## Work in progress

`git stash` is one stack for every worktree of the repository. Keep work in progress on your own branch, as a commit you amend or squash later.

## Teardown

`scripts/worktree.sh teardown` drops the worktree database and its test database. Orca runs it from `orca.yaml` when the app removes a worktree; `orca worktree rm` runs it only with `--run-hooks`.
