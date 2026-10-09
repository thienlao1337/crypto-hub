"""Technical signals: computation, issuing and accuracy statistics.

The default rule is an EMA crossover with an RSI filter. The indicator set is extended
through the rule's config, without schema migrations.

Every signal must explain itself: on the card the user sees not just "buy" but the
reason and the indicator values at the moment it fired.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Candle, Market, Signal, SignalOutcome, SignalRule, Timeframe
from app.models.signal import DIRECTION_BUY, DIRECTION_NEUTRAL, DIRECTION_SELL
from app.services import candle_service, indicators

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "ema_fast": 9,
    "ema_slow": 21,
    "rsi_period": 14,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
}

# How many candles indicators need to reach meaningful values.
MIN_CANDLES = 60


@dataclass(frozen=True)
class SignalDecision:
    direction: str
    reason: str
    indicators: dict
    price: Decimal
    candle_time: datetime


def analyse(candles: list[Candle], config: dict | None = None) -> SignalDecision | None:
    """Decision on the last closed candle.

    Pure function: no database, no network - so it's easy to check on made-up series,
    and the engine and the chart compute the same thing.
    """
    settings = {**DEFAULT_CONFIG, **(config or {})}

    # The last candle is still forming: deciding on it means issuing a signal
    # that vanishes if the price comes back before the period closes.
    closed = [candle for candle in candles if candle.is_closed]
    if len(closed) < MIN_CANDLES:
        return None

    closes = indicators.to_series([candle.close for candle in closed])
    fast = int(settings["ema_fast"])
    slow = int(settings["ema_slow"])
    rsi_period = int(settings["rsi_period"])

    crosses = indicators.ema_cross(closes, fast, slow)
    rsi_series = indicators.rsi(closes, rsi_period)

    cross = int(crosses.iloc[-1])
    rsi_value = indicators.last_value(rsi_series)
    ema_fast_value = indicators.last_value(indicators.ema(closes, fast))
    ema_slow_value = indicators.last_value(indicators.ema(closes, slow))

    last = closed[-1]
    values = {
        f"ema{fast}": ema_fast_value,
        f"ema{slow}": ema_slow_value,
        "rsi": rsi_value,
        "close": float(last.close),
    }

    overbought = float(settings["rsi_overbought"])
    oversold = float(settings["rsi_oversold"])

    if cross == 1:
        if rsi_value is not None and rsi_value >= overbought:
            direction = DIRECTION_NEUTRAL
            reason = (
                f"EMA{fast} пересекла EMA{slow} снизу вверх, но RSI {rsi_value:.1f} "
                f"выше порога перекупленности {overbought:.0f} — вход на этом уровне рискован."
            )
        else:
            direction = DIRECTION_BUY
            reason = (
                f"EMA{fast} пересекла EMA{slow} снизу вверх"
                + (f", RSI {rsi_value:.1f} — не перекуплен." if rsi_value is not None else ".")
            )
    elif cross == -1:
        if rsi_value is not None and rsi_value <= oversold:
            direction = DIRECTION_NEUTRAL
            reason = (
                f"EMA{fast} пересекла EMA{slow} сверху вниз, но RSI {rsi_value:.1f} "
                f"ниже порога перепроданности {oversold:.0f} — продавать поздно."
            )
        else:
            direction = DIRECTION_SELL
            reason = (
                f"EMA{fast} пересекла EMA{slow} сверху вниз"
                + (f", RSI {rsi_value:.1f} — не перепродан." if rsi_value is not None else ".")
            )
    else:
        return None

    return SignalDecision(
        direction=direction,
        reason=reason,
        indicators=values,
        price=last.close,
        candle_time=_as_utc(last.open_time),
    )


# --- Database operations ---


async def active_rules(session: AsyncSession) -> list[SignalRule]:
    result = await session.execute(
        select(SignalRule).where(SignalRule.is_active.is_(True)).order_by(SignalRule.id)
    )
    return list(result.scalars())


async def evaluate_rule(
    session: AsyncSession,
    rule: SignalRule,
    market: Market,
    timeframe: Timeframe,
) -> Signal | None:
    """Evaluate the rule for one pair and record a signal if it's new.

    A neutral verdict is recorded on a par with buy and sell: it doesn't appear on every
    candle, only when there was an EMA crossover but RSI is against entering. That's the
    most informative explanation the engine produces, and discarding it would show the
    user silence where the system actually thought it over and decided not to enter.
    """
    candles = await candle_service.stored_candles(session, market, timeframe, limit=300)
    decision = analyse(candles, rule.config)
    if decision is None:
        return None

    if await _already_emitted(session, rule.id, market.id, decision.candle_time):
        # The same signal on the same candle - the engine may have passed over it twice.
        return None

    signal = Signal(
        rule_id=rule.id,
        market_id=market.id,
        timeframe_id=timeframe.id,
        direction=decision.direction,
        price=decision.price,
        reason=decision.reason,
        indicators=decision.indicators,
        candle_time=decision.candle_time,
    )
    session.add(signal)
    await session.flush()
    return signal


async def recent_signals(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    limit: int = 50,
) -> list[tuple[Signal, str, str]]:
    """Latest signals together with pair and timeframe."""
    query = (
        select(Signal, Market.symbol, Timeframe.code)
        .join(Market, Market.id == Signal.market_id)
        .join(Timeframe, Timeframe.id == Signal.timeframe_id)
        .join(SignalRule, SignalRule.id == Signal.rule_id)
        .order_by(Signal.created_at.desc())
        .limit(limit)
    )
    if user_id is not None:
        # Rules are either shared (user_id empty) or personal.
        query = query.where(
            (SignalRule.user_id == user_id) | (SignalRule.user_id.is_(None))
        )

    result = await session.execute(query)
    return [(signal, symbol, code) for signal, symbol, code in result]


# --- Scoring whether a signal "played out" ---


async def evaluate_outcomes(session: AsyncSession) -> int:
    """Check old signals against the price once the horizon has passed.

    This is exactly the material for accuracy statistics: without an honest "how often
    did it work", a signal card is just an opinion.
    """
    now = datetime.now(timezone.utc)

    pending = await session.execute(
        select(Signal, SignalRule.evaluation_horizon_minutes)
        .join(SignalRule, SignalRule.id == Signal.rule_id)
        .outerjoin(SignalOutcome, SignalOutcome.signal_id == Signal.id)
        # A neutral verdict says nothing about direction, so it can't "come
        # true" either: it isn't counted in accuracy statistics. We filter it
        # out in the query rather than in the loop, otherwise such signals
        # would permanently occupy the sample window and crowd out the ones
        # that need scoring.
        .where(SignalOutcome.id.is_(None), Signal.direction != DIRECTION_NEUTRAL)
        .limit(500)
    )

    evaluated = 0
    for signal, horizon in pending:
        deadline = _as_utc(signal.candle_time) + timedelta(minutes=horizon)
        if deadline > now:
            continue

        price_after = await _price_at_horizon(session, signal.market_id, deadline)
        if price_after is None:
            # There's no candle at the horizon yet - nothing to score with.
            continue

        change = (price_after - signal.price) / signal.price * Decimal(100)
        is_success = change > 0 if signal.direction == DIRECTION_BUY else change < 0

        session.add(
            SignalOutcome(
                signal_id=signal.id,
                horizon_minutes=horizon,
                price_after=price_after,
                pnl_pct=change,
                is_success=is_success,
            )
        )
        evaluated += 1

    await session.flush()
    return evaluated


async def accuracy(session: AsyncSession, *, rule_id: int | None = None) -> dict:
    """Share of signals that moved in their direction."""
    query = select(
        func.count(SignalOutcome.id),
        func.sum(cast(SignalOutcome.is_success, Integer)),
        func.avg(SignalOutcome.pnl_pct),
    ).select_from(SignalOutcome)

    if rule_id is not None:
        query = query.join(Signal, Signal.id == SignalOutcome.signal_id).where(
            Signal.rule_id == rule_id
        )

    total, wins, avg_pnl = (await session.execute(query)).one()
    total = int(total or 0)
    wins = int(wins or 0)

    return {
        "total": total,
        "wins": wins,
        "win_rate": (wins / total * 100) if total else None,
        "avg_pnl_pct": float(avg_pnl) if avg_pnl is not None else None,
    }


async def _already_emitted(
    session: AsyncSession, rule_id: int, market_id: int, candle_time: datetime
) -> bool:
    result = await session.execute(
        select(Signal.id).where(
            Signal.rule_id == rule_id,
            Signal.market_id == market_id,
            Signal.candle_time == candle_time,
        )
    )
    return result.first() is not None


async def _price_at_horizon(
    session: AsyncSession, market_id: int, moment: datetime
) -> Decimal | None:
    """Price of the first candle at or after the horizon.

    Taking the last candle before the horizon is wrong: if there's no data for the
    required period, that would be the signal's own candle, and a made-up "0% change"
    result would land in the statistics.
    """
    result = await session.execute(
        select(Candle.close)
        .where(Candle.market_id == market_id, Candle.open_time >= moment)
        .order_by(Candle.open_time)
        .limit(1)
    )
    return result.scalar_one_or_none()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
