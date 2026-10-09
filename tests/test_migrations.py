"""Migrations must describe exactly what the models describe.

This isn't about neatness: the client deploys the database with migrations, while the
app runs on the models. If they diverge, the client's server ends up with a schema we
never had. Such a mismatch was found in this project twice: a missing nullable=False and
an extra default, and both times the hand-written migration looked perfectly plausible.

The test spins up a separate database, applies the whole migration chain to it and
compares the result with the metadata. It also checks that the chain can be walked
backwards: without a working downgrade the client would have no way to roll back a
failed release.
"""

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models import Base

MIGRATIONS_DB_SUFFIX = "_migrations"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# By default the comparison only looks at column presence and nullability. Type
# and default have to be enabled explicitly - that's exactly where the second
# mismatch was hiding.
COMPARE_OPTIONS = {
    "compare_type": True,
    "compare_server_default": True,
}


def _database_name() -> str:
    return f"{get_settings().postgres_db}{MIGRATIONS_DB_SUFFIX}"


def _url(database: str) -> str:
    settings = get_settings()
    return (
        f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{database}"
    )


async def _recreate_database(name: str) -> None:
    """An empty database for every run: leftovers from the past would distort the comparison."""
    engine = create_async_engine(_url("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", _url(_database_name()))
    return config


def _run_alembic(action, *args) -> None:
    """Alembic calls asyncio.run internally - its loop needs its own thread."""
    action(_alembic_config(), *args)


@pytest_asyncio.fixture
async def migrated_database(monkeypatch):
    # Compute the name before swapping the settings: afterwards
    # _database_name() would add the suffix on top of the suffix.
    name = _database_name()
    url = _url(name)

    await _recreate_database(name)

    # env.py builds the URL from the settings, not from alembic.ini, so those
    # are what need switching.
    monkeypatch.setattr(get_settings(), "postgres_db", name, raising=False)
    await asyncio.to_thread(_run_alembic, command.upgrade, "head")
    yield url


async def test_migrations_match_models(migrated_database):
    """The schema after migrations must match the models down to the column."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            diff = await connection.run_sync(
                lambda sync_connection: compare_metadata(
                    MigrationContext.configure(sync_connection, opts=COMPARE_OPTIONS),
                    Base.metadata,
                )
            )
    finally:
        await engine.dispose()

    assert diff == [], (
        "Схема после миграций разошлась с моделями. "
        "Различия (в терминах alembic): " + repr(diff)
    )


async def test_downgrade_removes_everything(migrated_database):
    """Without a working downgrade the client has no way to undo a failed release."""
    await asyncio.to_thread(_run_alembic, command.downgrade, "base")

    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            remaining = sorted(name for (name,) in rows)
    finally:
        await engine.dispose()

    # alembic doesn't drop its own service table - that's fine.
    assert remaining == ["alembic_version"]


async def test_chain_is_reapplyable(migrated_database):
    """Downgrade followed by upgrade is a normal release debugging scenario."""
    await asyncio.to_thread(_run_alembic, command.downgrade, "base")
    await asyncio.to_thread(_run_alembic, command.upgrade, "head")

    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            tables = {name for (name,) in rows}
    finally:
        await engine.dispose()

    assert "users" in tables and "bot_orders" in tables


@pytest.mark.parametrize("table", ["exchanges", "timeframes", "alert_types"])
async def test_reference_data_is_seeded(migrated_database, table):
    """Reference data arrives via migration: empty, it makes the panel unusable."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            count = await connection.scalar(text(f"SELECT count(*) FROM {table}"))
    finally:
        await engine.dispose()

    assert count > 0
