# 37. Package and CLI renamed

Status: Accepted — 2026-11-01. Amends ADR 36's decision that internal names stay. Its decision to keep the stack-identity names is superseded by ADR 38.

## Context
ADR 36 renamed the repository to `football-data-ai-platform` and kept every internal name. That left the code and the repository with different names. Readers see two of those internal names: contributors see the Python package, and operators see the CLI.

The project is early: no one depends on the CLI outside this repository, and no pull request was open when this change was made.

## Decision
- **The Python package is `football_data_ai_platform`** (`src/football_data_ai_platform/`), and the distribution is `football-data-ai-platform`.
- **The CLI is `fdap`,** the initials of `football-data-ai-platform`. It replaces the old CLI outright; no alias is kept.
- The stack-identity names (Compose project, database, dbt and Langfuse) were kept at first. ADR 38 renamed them too.

## Consequences
- Imports read `from football_data_ai_platform.… import …`, and the worker runs as `python -m football_data_ai_platform.ingestion.worker`. Temporal workflow and activity type names come from class and method names, so running workflows and Schedules are unaffected.
- Commands are `uv run fdap …`. After pulling, `uv sync` (or any `uv run`) removes the old script from the virtualenv.
