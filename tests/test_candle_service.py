from datetime import timedelta

import pytest_asyncio
from sqlalchemy import select

from app.models import Candle, Exchange, Timeframe
from app.services import candle_service, market_service
from tests import fakes


@pytest_asyncio.fixture
async def market(session):
    exchange = Exchange(code="bybit", name="Bybit", sort_order=10)
    session.add(exchange)
    session.add(Timeframe(code="1h", label="1 час", seconds=3600, sort_order=40))
    await session.flush()

    await market_service.sync_markets(
        session,
        exchange,
        fakes.FakeAdapter(markets=[fakes.market("BTC/USDT", "BTC", "USDT")]),
    )
    await session.commit()

    return await market_service.get_market(session, exchange.id, "BTC/USDT")


@pytest_asyncio.fixture
async def hour(session):
    return await candle_service.get_timeframe(session, "1h")


# --- Загрузка ---


async def test_sync_stores_candles(session, market, hour):
    adapter = fakes.FakeAdapter(bars=fakes.hourly_bars(5))

    saved = await candle_service.sync_candles(session, market, hour, adapter)
    await session.commit()

    assert saved == 5
    rows = (await session.execute(select(Candle))).scalars().all()
    assert len(rows) == 5


async def test_sync_is_idempotent(session, market, hour):
    bars = fakes.hourly_bars(5)

    await candle_service.sync_candles(session, market, hour, fakes.FakeAdapter(bars=bars))
    await session.commit()
    saved = await candle_service.sync_candles(session, market, hour, fakes.FakeAdapter(bars=bars))
    await session.commit()

    assert saved == 0
    rows = (await session.execute(select(Candle))).scalars().all()
    assert len(rows) == 5


async def test_last_candle_is_open_and_gets_updated(session, market, hour):
    """Свеча текущего периода ещё формируется и должна перезаписываться."""
    bars = fakes.hourly_bars(3)
    await candle_service.sync_candles(session, market, hour, fakes.FakeAdapter(bars=bars))
    await session.commit()

    rows = await candle_service.stored_candles(session, market, hour)
    assert [row.is_closed for row in rows] == [True, True, False]

    # Тот же период, но цена ушла дальше.
    changed = list(bars)
    changed[-1] = fakes.bar(bars[-1].open_time, "999")
    await candle_service.sync_candles(session, market, hour, fakes.FakeAdapter(bars=changed))
    await session.commit()

    rows = await candle_service.stored_candles(session, market, hour)
    assert len(rows) == 3
    assert float(rows[-1].close) == 999.0


async def test_closed_candle_is_not_rewritten(session, market, hour):
    """У закрытой свечи значения окончательны."""
    bars = fakes.hourly_bars(3)
    await candle_service.sync_candles(session, market, hour, fakes.FakeAdapter(bars=bars))
    await session.commit()

    tampered = list(bars)
    tampered[0] = fakes.bar(bars[0].open_time, "1")
    await candle_service.sync_candles(session, market, hour, fakes.FakeAdapter(bars=tampered))
    await session.commit()

    rows = await candle_service.stored_candles(session, market, hour)
    assert float(rows[0].close) == 100.0


async def test_freshness_check(session, market, hour):
    assert not await candle_service.is_fresh(session, market, hour)

    from datetime import datetime, timezone

    session.add(
        Candle(
            market_id=market.id,
            timeframe_id=hour.id,
            open_time=datetime.now(timezone.utc) - timedelta(minutes=5),
            open=1, high=1, low=1, close=1, volume=1,
        )
    )
    await session.commit()

    assert await candle_service.is_fresh(session, market, hour)


async def test_stale_data_survives_exchange_failure(session, market, hour):
    """Отказ биржи не должен оставлять пользователя с пустым графиком."""
    await candle_service.sync_candles(
        session, market, hour, fakes.FakeAdapter(bars=fakes.hourly_bars(5))
    )
    await session.commit()

    broken = fakes.FakeAdapter(raise_on="fetch_ohlcv")
    candles = await candle_service.candles_for_chart(
        session, market, hour, adapter_factory=lambda: broken
    )

    assert len(candles) == 5
    assert broken.closed, "подключение к бирже закрывается даже при ошибке"


# --- Индикаторы для графика ---


async def test_indicator_series_align_with_candles(session, market, hour):
    """Ряды индикаторов должны покрывать все свечи.

    Пропуск начальных точек сдвигал бы панель RSI относительно цены.
    """
    await candle_service.sync_candles(
        session, market, hour, fakes.FakeAdapter(bars=fakes.hourly_bars(60))
    )
    await session.commit()
    candles = await candle_service.stored_candles(session, market, hour)

    result = candle_service.compute_indicators(candles, {"ema": [9], "rsi": 14})

    assert len(result["ema9"]) == len(candles)
    assert len(result["rsi"]) == len(candles)
    # Первые точки RSI пустые: истории ещё не хватает.
    assert "value" not in result["rsi"][0]
    assert "value" in result["rsi"][-1]


async def test_chart_payload_shape(session, market, hour):
    await candle_service.sync_candles(
        session, market, hour, fakes.FakeAdapter(bars=fakes.hourly_bars(3))
    )
    await session.commit()
    candles = await candle_service.stored_candles(session, market, hour)

    payload = candle_service.candles_to_chart(candles)

    assert set(payload[0]) == {"time", "open", "high", "low", "close", "volume"}
    assert isinstance(payload[0]["time"], int)
    assert payload[0]["close"] == 100.0


async def test_no_indicators_requested(session, market, hour):
    await candle_service.sync_candles(
        session, market, hour, fakes.FakeAdapter(bars=fakes.hourly_bars(3))
    )
    await session.commit()
    candles = await candle_service.stored_candles(session, market, hour)

    assert candle_service.compute_indicators(candles, {}) == {}
    assert candle_service.compute_indicators([], {"ema": [9]}) == {}
