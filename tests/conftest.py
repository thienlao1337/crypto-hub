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
    """Отдельная база под тесты — рядом с рабочей, но не она.

    Тесты гоняются на PostgreSQL, а не на SQLite: на SQLite тип Numeric
    хранится как float, и 1234.56 читается обратно как
    1234.559999999999945430. Для продукта, который считает деньги, такой
    стенд бесполезен — он и прячет настоящие ошибки, и выдумывает свои.
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
    """Схема разворачивается один раз на прогон."""
    await _ensure_database_exists()

    engine = create_async_engine(_test_database_url(), poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncSession:
    """Сессия внутри внешней транзакции, которая откатывается после теста.

    Пересоздавать тридцать таблиц на каждый тест дорого, поэтому тест
    работает в транзакции, а его commit'ы становятся точками сохранения
    внутри неё (join_transaction_mode="create_savepoint").
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
    """Свой ключ шифрования на каждый тест.

    Тесты не должны зависеть от .env разработчика и не должны шифровать
    тестовые данные боевым ключом.
    """
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(security.get_settings(), "encryption_key", key, raising=False)
    security._fernet.cache_clear()
    yield key
    security._fernet.cache_clear()


@pytest_asyncio.fixture
async def owner(session):
    """Владелец системы — он выдаёт приглашения."""
    from app.services import user_service

    user = await user_service.create_user(
        session,
        email="owner@example.com",
        password="owner-password-1",
        role="owner",
    )
    await session.commit()
    return user
