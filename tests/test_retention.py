"""Уборка старых данных.

Проверяется не только «удалилось», но и «нужное осталось»: чистка,
которая уносит свежие свечи, ломает и графики, и индикаторы, а заметно
это станет через сутки после установки.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import func, select

from app.config import get_settings
from app.models import Candle, Exchange, GlobalStats, LoginEvent, Market, Timeframe
from app.services import market_service
from app.worker import retention
from tests import fakes

NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def setup(session, monkeypatch):
    """Две пары на двух таймфреймах и немного старых записей."""

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(retention, "session_scope", scope)

    exchange = Exchange(code="bybit", name="Bybit", sort_order=10)
    session.add(exchange)
    minute = Timeframe(code="1m", label="1 минута", seconds=60, sort_order=10)
    daily = Timeframe(code="1d", label="1 день", seconds=86400, sort_order=60)
    session.add_all([minute, daily])
    await session.flush()

    await market_service.sync_markets(
        session,
        exchange,
        fakes.FakeAdapter(
            markets=[
                fakes.market("BTC/USDT", "BTC", "USDT"),
                fakes.market("ETH/USDT", "ETH", "USDT"),
            ]
        ),
    )
    btc = await market_service.get_market(session, exchange.id, "BTC/USDT")
    eth = await market_service.get_market(session, exchange.id, "ETH/USDT")
    await session.flush()

    return {"btc": btc, "eth": eth, "minute": minute, "daily": daily}


def add_candles(session, market: Market, timeframe: Timeframe, count: int) -> None:
    for index in range(count):
        value = Decimal(100 + index)
        session.add(
            Candle(
                market_id=market.id,
                timeframe_id=timeframe.id,
                open_time=NOW - timedelta(minutes=count - index),
                open=value, high=value, low=value, close=value,
                volume=Decimal(1), is_closed=True,
            )
        )


async def count_candles(session, market: Market, timeframe: Timeframe) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Candle)
            .where(Candle.market_id == market.id, Candle.timeframe_id == timeframe.id)
        )
    )


# --- Свечи ---


async def test_candles_are_trimmed_per_series(session, setup, monkeypatch):
    """Ограничение считается на каждую пару и таймфрейм отдельно."""
    monkeypatch.setattr(get_settings(), "candles_keep_per_series", 10, raising=False)

    add_candles(session, setup["btc"], setup["minute"], 25)
    add_candles(session, setup["btc"], setup["daily"], 25)
    add_candles(session, setup["eth"], setup["minute"], 5)
    await session.commit()

    await retention.cleanup()

    assert await count_candles(session, setup["btc"], setup["minute"]) == 10
    assert await count_candles(session, setup["btc"], setup["daily"]) == 10
    # Короткий ряд трогать нечего — иначе график этой пары опустел бы.
    assert await count_candles(session, setup["eth"], setup["minute"]) == 5


async def test_newest_candles_survive(session, setup, monkeypatch):
    """Уносить надо старое: на свежем держатся и график, и индикаторы."""
    monkeypatch.setattr(get_settings(), "candles_keep_per_series", 3, raising=False)

    add_candles(session, setup["btc"], setup["minute"], 10)
    await session.commit()

    await retention.cleanup()

    rows = (
        await session.execute(
            select(Candle.close)
            .where(Candle.market_id == setup["btc"].id)
            .order_by(Candle.open_time)
        )
    ).all()

    assert [value for (value,) in rows] == [Decimal(107), Decimal(108), Decimal(109)]


# --- Записи по возрасту ---


async def test_old_login_events_are_removed(session, setup, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_events_keep_days", 30, raising=False)

    now = datetime.now(timezone.utc)
    session.add(LoginEvent(email="a@example.com", is_success=True, created_at=now))
    session.add(
        LoginEvent(
            email="b@example.com",
            is_success=False,
            created_at=now - timedelta(days=90),
        )
    )
    await session.commit()

    await retention.cleanup()

    remaining = (await session.execute(select(LoginEvent.email))).scalars().all()
    assert remaining == ["a@example.com"]


async def test_zero_days_means_keep_forever(session, setup, monkeypatch):
    """Клиент может захотеть хранить журнал входов дольше умолчания."""
    monkeypatch.setattr(get_settings(), "login_events_keep_days", 0, raising=False)

    session.add(
        LoginEvent(
            email="старый@example.com",
            is_success=True,
            created_at=datetime.now(timezone.utc) - timedelta(days=900),
        )
    )
    await session.commit()

    await retention.cleanup()

    assert await session.scalar(select(func.count()).select_from(LoginEvent)) == 1


async def test_old_market_stats_are_removed(session, setup, monkeypatch):
    monkeypatch.setattr(get_settings(), "global_stats_keep_days", 10, raising=False)

    now = datetime.now(timezone.utc)
    session.add(GlobalStats(total_market_cap_usd=Decimal(1), captured_at=now))
    session.add(
        GlobalStats(
            total_market_cap_usd=Decimal(2), captured_at=now - timedelta(days=400)
        )
    )
    await session.commit()

    await retention.cleanup()

    assert await session.scalar(select(func.count()).select_from(GlobalStats)) == 1


async def test_cleanup_survives_a_broken_run(session, setup, monkeypatch):
    """Сбой уборки не должен ронять планировщик целиком."""

    @asynccontextmanager
    async def broken():
        raise RuntimeError("база недоступна")
        yield  # pragma: no cover

    monkeypatch.setattr(retention, "session_scope", broken)

    assert await retention.cleanup() == {}
