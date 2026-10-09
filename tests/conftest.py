import base64
import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models import Base
from app.services import security

TEST_DB_SUFFIX = "_test"


def _test_database_url() -> str:
    """A separate database for tests - next to the working one, but not it.

    Tests run on PostgreSQL, not SQLite: SQLite stores the Numeric type as float, and
    1234.56 is read back as 1234.559999999999945430. For a product that handles money
    such a test bed is useless - it both hides real bugs and invents its own.
    """
    settings = get_settings()
    return (
        f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}"
        f"/{settings.postgres_db}{TEST_DB_SUFFIX}"
    )


async def _ensure_database_exists() -> None:
    settings = get_settings()
    admin_url = (
        f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/postgres"
    )
    name = f"{settings.postgres_db}{TEST_DB_SUFFIX}"

    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            from sqlalchemy import text

            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine():
    """The schema is created once per run."""
    await _ensure_database_exists()

    engine = create_async_engine(_test_database_url(), poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncSession:
    """A session inside an outer transaction that is rolled back after the test.

    Recreating thirty tables for every test is expensive, so a test runs inside a
    transaction and its commits become savepoints within it
    (join_transaction_mode="create_savepoint").
    """
    connection = await engine.connect()
    transaction = await connection.begin()

    factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        class_=AsyncSession,
        join_transaction_mode="create_savepoint",
    )
    async with factory() as session:
        yield session

    await transaction.rollback()
    await connection.close()


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    """A separate encryption key for every test.

    Tests must not depend on the developer's .env and must not encrypt test data with
    the production key.
    """
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(security.get_settings(), "encryption_key", key, raising=False)
    security._fernet.cache_clear()
    yield key
    security._fernet.cache_clear()


@pytest_asyncio.fixture
async def owner(session):
    """The system owner - the one who issues invites."""
    from app.services import user_service

    user = await user_service.create_user(
        session,
        email="owner@example.com",
        password="owner-password-1",
        role="owner",
    )
    await session.commit()
    return user
