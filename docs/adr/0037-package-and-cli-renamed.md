# 37. Package and CLI renamed; stack-identity names stay

Status: Accepted — 2026-11-01. Amends ADR 36's decision that internal names stay. Stack-identity names superseded by ADR 38.

## Context
ADR 36 renamed the repository to `football-data-ai-platform` and kept every internal name `football-intel` / `football_intel`. That left the code and the repository with different names. Readers see two of those internal names: contributors see the Python package, and operators see the CLI. Nobody reads the others (the Compose project, the Postgres database and role, the dbt project and the Langfuse organisation), and two of them key live data: the Compose project name prefixes every Docker volume, and the database name holds the live data.

The project is early: no one depends on the CLI outside this repository, and no pull request was open when this change was made.

## Decision
- **The Python package is `football_data_ai_platform`** (`src/football_data_ai_platform/`), and the distribution is `football-data-ai-platform`.
- **The CLI is `fdap`,** the initials of `football-data-ai-platform`. It replaces `football-intel` outright; no alias is kept.
- **The stack-identity names stay `football-intel` / `football_intel`:** the Compose project and its volumes, the Postgres database and role (and the `football_intel_wt_<slug>` worktree databases), the dbt project and profile, and the Langfuse organisation and project. Renaming them risks live data and changes nothing a reader sees.

## Consequences
- Imports read `from football_data_ai_platform.… import …`, and the worker runs as `python -m football_data_ai_platform.ingestion.worker`. Temporal workflow and activity type names come from class and method names, so running workflows and Schedules are unaffected.
- `uv run fdap …` replaces `uv run football-intel …`. After pulling, `uv sync` (or any `uv run`) removes the old script from the virtualenv. ADRs 29–32 keep the old CLI name as a record of their time.
- `compose.yaml`, `.env`, `dbt/`, CI and `scripts/worktree.sh` still say `football-intel` / `football_intel`. A future rename of those names needs its own ADR, a volume backup, and pinned volume names (issue #91, Phase B).
