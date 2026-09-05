import base64
import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base
from app.services import security


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
async def engine():
    """Чистая SQLite-база в памяти на каждый тест.

    Сервисный слой не использует специфику PostgreSQL, а типы колонок,
    которым она нужна (JSONB, BIGSERIAL), объявлены через with_variant —
    см. app/models/types.py.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session


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
