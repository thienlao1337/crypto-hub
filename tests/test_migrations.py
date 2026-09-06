"""Миграции должны описывать ровно то, что описывают модели.

Проверка не про красоту: клиент разворачивает базу миграциями, а
приложение работает по моделям. Разошлись — и на его сервере окажется
схема, которой у нас никогда не было. Такое расхождение находилось в этом
проекте дважды: пропущенный nullable=False и лишнее значение по умолчанию,
и оба раза руками написанная миграция выглядела совершенно правдоподобно.

Тест поднимает отдельную базу, накатывает на неё всю цепочку миграций и
сравнивает результат с metadata. Заодно проверяет, что цепочка проходится
и в обратную сторону: без работающего downgrade откатить неудачный релиз
у клиента будет нечем.
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

# Сравнение по умолчанию смотрит только на наличие столбцов и их
# обязательность. Тип и значение по умолчанию приходится включать явно —
# ровно там и пряталось второе расхождение.
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
    """Пустая база на каждый прогон: остатки прошлого исказили бы сравнение."""
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
    """Alembic внутри зовёт asyncio.run — своему циклу нужен свой поток."""
    action(_alembic_config(), *args)


@pytest_asyncio.fixture
async def migrated_database(monkeypatch):
    # Имя считаем до подмены настроек: после неё _database_name() выдал бы
    # суффикс поверх суффикса.
    name = _database_name()
    url = _url(name)

    await _recreate_database(name)

    # env.py собирает адрес из настроек, а не из alembic.ini, поэтому
    # переключать надо именно их.
    monkeypatch.setattr(get_settings(), "postgres_db", name, raising=False)
    await asyncio.to_thread(_run_alembic, command.upgrade, "head")
    yield url


async def test_migrations_match_models(migrated_database):
    """Схема после миграций должна совпадать с моделями до столбца."""
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
    """Без работающего отката неудачный релиз у клиента нечем отменить."""
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

    # Своей служебной таблицы alembic не удаляет — это нормально.
    assert remaining == ["alembic_version"]


async def test_chain_is_reapplyable(migrated_database):
    """Откат и повторный накат подряд — обычный сценарий отладки релиза."""
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
    """Справочники приезжают миграцией: пустыми они делают панель нерабочей."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            count = await connection.scalar(text(f"SELECT count(*) FROM {table}"))
    finally:
        await engine.dispose()

    assert count > 0
