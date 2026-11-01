# 29. Database migrations: Alembic with plain SQL

Status: Accepted — 2026-10-12. Amended by ADR 31 (identity schema).

## Context
The raw schema (Raw Responses, ADR 28) needs versioned, repeatable schema changes, applied the same way on a developer machine, in CI and later on Neon (ADR 22). dbt (ADR 11) owns staging and marts, so migrations only cover the tables that ingestion writes. Options weighed:

- **Alembic** — the standard Python migration tool; runs from the project's own environment, so tests and CI migrate a database with a function call. Brings SQLAlchemy, which we use only as a connection layer.
- **dbmate / sqitch** — plain SQL and language-neutral, but a separate binary to install locally and in CI, and no in-process API for test fixtures.
- **yoyo-migrations** — plain SQL with a Python API, but a small project with an uncertain maintenance outlook.
- **Hand-rolled runner** — no dependency, but we would own ordering, locking and history ourselves.

## Decision
- Alembic manages the schema of the application database. Migrations live in `migrations/versions/`, numbered `0001_…`, `0002_…`.
- Migrations are plain SQL through `op.execute`. There are no ORM models and no autogenerate; application code talks to Postgres through `psycopg`.
- Alembic reads the connection from `football_data_ai_platform.config.Settings` (the `.env` variables), unless `sqlalchemy.url` is set explicitly, as the test fixtures do.
- In compose, a one-shot `migrate` service runs `alembic upgrade head`; the worker starts only after it succeeds.
- Ingestion-owned tables live in the `raw` schema. dbt models (staging, marts) are not migrations.

## Consequences
- Tests create a fresh database and migrate it to head before running.
- Every schema change ships with a migration in the same pull request.
- SQLAlchemy is a dependency only for Alembic.
