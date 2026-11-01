# 38. Stack identity renamed to football-data-ai-platform; Langfuse stays

Status: Accepted — 2026-11-01. Supersedes ADR 37's decision that the stack-identity names stay.

## Context
ADR 37 renamed the package and the CLI and kept the stack-identity names `football-intel` / `football_intel`, because they key live data. That left the old name on every container (`football-intel-worker-1`), on the volumes, on the database and in the dbt project. Operators see those names every time they run `docker ps` or `psql`, so they still contradict the repository's name.

The Compose project name prefixes every volume. Renaming the project alone would start an empty stack. Pinning each volume to its old name (`name: football-intel_postgres-data`) keeps the data, but Compose v5 then warns on every `up`, once per volume, that the volume belongs to another project. The old name would also stay in `compose.yaml`.

## Decision
- **The Compose project is `football-data-ai-platform`.** Volumes are migrated, not pinned: the stack is stopped, `docker compose create` makes the new volumes, and each old volume's contents are copied into its new one. The old volumes are kept, untouched, until the migrated stack is verified, and are then removed.
- **The Postgres database and role are `football_data_ai_platform`.** They are renamed in place (`ALTER DATABASE … RENAME`, `ALTER ROLE … RENAME`), with no other connections open, by a temporary superuser that is dropped afterwards. The role keeps its SCRAM password.
- **Worktree databases are `football_data_ai_platform_wt_<slug>`.** The prefix grows from 18 to 29 characters, so the slug is cut to 19 characters, down from 30. That keeps 15 characters for test suffixes under Postgres's 63-byte limit (#85).
- **The dbt project and profile are `football_data_ai_platform`.** Relations do not change, because schemas come from the profile and `+schema`.
- **Langfuse keeps `football-intel`.** Its `LANGFUSE_INIT_*` values apply only on first start, and changing them risks a second project that would split the trace history. Display names can be renamed in the Langfuse UI.

## Consequences
- After pulling, each checkout updates `POSTGRES_DB` and `POSTGRES_USER` in its `.env`. Worktrees run `scripts/worktree.sh teardown`, then `setup`.
- The backup procedure's volume filter and archive names now use `football-data-ai-platform`.
- Rollback: restore the old `name:` in `compose.yaml`, rename the database and role back, and start the stack on the kept volumes.
