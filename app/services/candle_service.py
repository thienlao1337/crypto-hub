"""Candles: storage, backfill from the exchange and indicator calculation on them.

The chart and the signal engine take data from here rather than each going to the
exchange: otherwise a dozen open tabs would use up the request limit, and signals would
be computed on slightly different data than the user sees.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exchanges.base import ExchangeAdapter
from app.models import Candle, Market, Timeframe
from app.services import indicators

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 500
# Freshness margin: if the latest candle is younger than this, we don't go to the exchange.
STALE_FACTOR = 1.0


async def get_timeframe(session: AsyncSession, code: str) -> Timeframe | None:
    result = await session.execute(select(Timeframe).where(Timeframe.code == code))
    return result.scalar_one_or_none()


async def list_timeframes(session: AsyncSession) -> list[Timeframe]:
    result = await session.execute(
        select(Timeframe).where(Timeframe.is_active.is_(True)).order_by(Timeframe.sort_order)
    )
    return list(result.scalars())


async def stored_candles(
    session: AsyncSession,
    market: Market,
    timeframe: Timeframe,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[Candle]:
    """Latest candles from the database, in chronological order."""
    result = await session.execute(
        select(Candle)
        .where(Candle.market_id == market.id, Candle.timeframe_id == timeframe.id)
        .order_by(Candle.open_time.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def is_fresh(session: AsyncSession, market: Market, timeframe: Timeframe) -> bool:
    """Whether the database has a candle for the current period."""
    last_time = await session.scalar(
        select(Candle.open_time)
        .where(Candle.market_id == market.id, Candle.timeframe_id == timeframe.id)
        .order_by(Candle.open_time.desc())
        .limit(1)
    )
    if last_time is None:
        return False

    if last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last_time
    return age < timedelta(seconds=timeframe.seconds * (1 + STALE_FACTOR))


async def sync_candles(
    session: AsyncSession,
    market: Market,
    timeframe: Timeframe,
    adapter: ExchangeAdapter,
    *,
    limit: int = DEFAULT_LIMIT,
) -> int:
    """Backfill candles from the exchange and write the missing ones.

    The period's last candle isn't closed yet and keeps changing, so it is overwritten
    on every pass, while closed ones are only appended.
    """
    bars = await adapter.fetch_ohlcv(market.symbol, timeframe.code, limit=limit)
    if not bars:
        return 0

    existing = await session.execute(
        select(Candle).where(
            Candle.market_id == market.id,
            Candle.timeframe_id == timeframe.id,
            Candle.open_time >= bars[0].open_time,
        )
    )
    by_time = {_as_utc(row.open_time): row for row in existing.scalars()}

    # The current period's candle is still forming: the exchange returns it last.
    last_open_time = bars[-1].open_time
    saved = 0

    for bar in bars:
        row = by_time.get(bar.open_time)
        is_closed = bar.open_time < last_open_time

        if row is None:
            session.add(
                Candle(
                    market_id=market.id,
                    timeframe_id=timeframe.id,
                    open_time=bar.open_time,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    is_closed=is_closed,
                )
            )
            saved += 1
        elif not row.is_closed:
            # Update only the open candle: a closed one has final values.
            row.open = bar.open
            row.high = bar.high
            row.low = bar.low
            row.close = bar.close
            row.volume = bar.volume
            row.is_closed = is_closed

    await session.flush()
    return saved


async def candles_for_chart(
    session: AsyncSession,
    market: Market,
    timeframe: Timeframe,
    adapter_factory=None,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[Candle]:
    """Candles for rendering: from the database, backfilled if needed.

    adapter_factory is passed separately so the service doesn't know about ccxt and
    stays testable without the network.
    """
    if adapter_factory is not None and not await is_fresh(session, market, timeframe):
        adapter = adapter_factory()
        try:
            await sync_candles(session, market, timeframe, adapter, limit=limit)
        except Exception as exc:
            # Stale candles are better than an empty chart: show what we have.
            logger.warning("Could not backfill candles for %s: %s", market.symbol, exc)
        finally:
            await adapter.close()

    return await stored_candles(session, market, timeframe, limit=limit)


def compute_indicators(candles: list[Candle], config: dict) -> dict:
    """Compute the selected indicators over a candle series.

    Returns series ready for rendering: values are aligned to candles, with None for the
    missing ones at the start, so the client doesn't have to guess the offset.
    """
    if not candles:
        return {}

    closes = indicators.to_series([candle.close for candle in candles])
    times = [int(_as_utc(candle.open_time).timestamp()) for candle in candles]
    result: dict = {}

    for period in config.get("ema", []):
        result[f"ema{period}"] = _points(times, indicators.ema(closes, period))

    for period in config.get("sma", []):
        result[f"sma{period}"] = _points(times, indicators.sma(closes, period))

    if config.get("rsi"):
        period = config["rsi"] if isinstance(config["rsi"], int) else 14
        result["rsi"] = _points(times, indicators.rsi(closes, period))

    if config.get("macd"):
        macd = indicators.macd(closes)
        result["macd"] = _points(times, macd.macd)
        result["macd_signal"] = _points(times, macd.signal)
        result["macd_histogram"] = _points(times, macd.histogram)

    if config.get("bollinger"):
        bands = indicators.bollinger(closes)
        result["bb_upper"] = _points(times, bands.upper)
        result["bb_middle"] = _points(times, bands.middle)
        result["bb_lower"] = _points(times, bands.lower)

    return result


def _points(times: list[int], series) -> list[dict]:
    """A series as chart points.

    Where there's no value yet (the indicator lacked history) we return a point without
    value. The charting library treats such points as empty and keeps the time axis for
    them: otherwise RSI(14) would start fourteen candles to the right of the price, and
    the panels below the chart would show a different time range than the candles above
    them.
    """
    points = []
    for moment, value in zip(times, series):
        if value is None or pd.isna(value):
            points.append({"time": moment})
        else:
            points.append({"time": moment, "value": float(value)})
    return points


def candles_to_chart(candles: list[Candle]) -> list[dict]:
    return [
        {
            "time": int(_as_utc(candle.open_time).timestamp()),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "volume": float(candle.volume),
        }
        for candle in candles
    ]


def last_close(candles: list[Candle]) -> Decimal | None:
    return candles[-1].close if candles else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
