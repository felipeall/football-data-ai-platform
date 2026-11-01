"""Alembic environment. Migrations are plain SQL; there are no ORM models (ADR 29)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from football_data_ai_platform.config import Settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)


def database_url() -> str:
    """SQLAlchemy URL: an explicit `sqlalchemy.url`, else the configured application database."""
    url = config.get_main_option("sqlalchemy.url") or Settings().database_url
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_online() -> None:
    engine = create_engine(database_url())
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported; run against a database.")
run_migrations_online()
