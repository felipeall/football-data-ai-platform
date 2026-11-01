import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection, sql
from temporalio.testing import WorkflowEnvironment

from football_data_ai_platform.config import Settings


@pytest.fixture(scope="session")
def worker(worker_id: str) -> str:
    """The pytest-xdist worker's number (`3` for `gw3`), empty without xdist. Tests append
    it to the databases they create, so workers running in parallel never share one."""
    return worker_id.removeprefix("gw") if worker_id != "master" else ""


@pytest.fixture(scope="session")
def database_url(worker: str) -> Iterator[str]:
    """A fresh test database next to the application database, migrated to head, one per
    pytest-xdist worker.

    Skipped when Postgres is unreachable, except in CI, where it must be there.
    """
    settings = Settings()
    test_db = f"{settings.postgres_db}_test{worker}"
    try:
        admin = psycopg.connect(settings.database_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as error:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"Postgres unreachable ({error}); start it with docker compose up --wait")
    with admin:
        name = sql.Identifier(test_db)
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(name))
    url = settings.model_copy(update={"postgres_db": test_db}).database_url

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    yield url


@pytest.fixture
async def connection(database_url: str) -> AsyncIterator[AsyncConnection[Any]]:
    """An autocommit connection to empty Raw Responses, Fetch Attempts, Players, Identity
    Candidates and Clubs. A Candidate left by one test would point at a record the next one's
    staging lacks, and fail dbt's check that staging holds every Candidate's Source Record.
    Competitions and Stages stay: the migration enters them."""
    async with await AsyncConnection.connect(database_url, autocommit=True) as conn:
        await conn.execute("TRUNCATE raw.raw_responses, raw.fetch_attempts")
        await conn.execute("TRUNCATE identity.identity_candidates, identity.players")
        await conn.execute("TRUNCATE identity.club_links, identity.club_aliases, identity.clubs")
        yield conn


@pytest.fixture(scope="session")
async def temporal() -> AsyncIterator[WorkflowEnvironment]:
    # The SDK's signature has a partially unknown parameter type.
    async with await WorkflowEnvironment.start_local() as env:  # pyright: ignore[reportUnknownMemberType]
        yield env


@pytest.fixture(scope="session")
async def time_skipping() -> AsyncIterator[WorkflowEnvironment]:
    # Skips the retry policy's backoff instead of waiting it out.
    async with await WorkflowEnvironment.start_time_skipping() as env:  # pyright: ignore[reportUnknownMemberType]
        yield env
