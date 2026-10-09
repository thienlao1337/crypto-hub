"""Signal engine tests.

The decision is separate from the database, so most of it can be tested on made-up price
series - there it's clear what exactly caused the signal.

The series are chosen so the crossover lands exactly on the last candle: on every run
the engine looks at the freshest closed candle, and it would no longer see a signal from
the middle of the series.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import select

from app.models import Candle, Exchange, Signal, SignalOutcome, SignalRule, Timeframe
from app.models.signal import DIRECTION_BUY, DIRECTION_NEUTRAL, DIRECTION_SELL
from app.services import market_service, signal_service
from tests import fakes

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Sawtooth around 100: RSI stays in the middle of the range instead of hitting the edge.
FLAT_WAVE = [round(100 + (3 if i % 2 else -3) + (i % 5) * 0.4, 2) for i in range(70)]
# The same sawtooth drifting down: by the end the fast EMA is below the slow one.
SLIDING_DOWN = [round(100 - i * 0.15 + (2 if i % 2 else -2), 2) for i in range(70)]


def extend(prices: list[float], step: float, count: int) -> list[float]:
    result = list(prices)
    for _ in range(count):
        result.append(round(result[-1] + step, 2))
    return result


# Upward reversal: crossover and buy on the last candle, RSI around 53.
BUY_SERIES = extend(SLIDING_DOWN, 0.9, 3)
# Downward reversal: sell on the last candle, RSI around 49.
SELL_SERIES = extend(FLAT_WAVE, -0.9, 6)


def make_candles(prices, *, open_tail: bool = False) -> list[Candle]:
    """Candles straight into memory: analyse reads the price and the closed flag."""
    candles = []
    for index, price in enumerate(prices):
        value = Decimal(str(price))
        candles.append(
            Candle(
                market_id=1,
                timeframe_id=1,
                open_time=BASE_TIME + timedelta(hours=index),
                open=value, high=value, low=value, close=value, volume=Decimal(1),
                is_closed=True,
            )
        )
    if open_tail:
        candles[-1].is_closed = False
    return candles


# --- Pure decision ---


def test_no_signal_without_enough_history():
    assert signal_service.analyse(make_candles([100] * 10)) is None


def test_no_signal_when_nothing_crosses():
    assert signal_service.analyse(make_candles([100] * 80)) is None


def test_buy_on_upward_cross():
    decision = signal_service.analyse(make_candles(BUY_SERIES))

    assert decision is not None
    assert decision.direction == DIRECTION_BUY
    assert "снизу вверх" in decision.reason
    assert 40 < decision.indicators["rsi"] < 70


def test_sell_on_downward_cross():
    decision = signal_service.analyse(make_candles(SELL_SERIES))

    assert decision is not None
    assert decision.direction == DIRECTION_SELL
    assert "сверху вниз" in decision.reason


def test_overbought_cross_is_downgraded_to_neutral():
    """An upward crossover with overbought RSI is no reason to buy."""
    decision = signal_service.analyse(make_candles(BUY_SERIES), {"rsi_overbought": 50})

    assert decision.direction == DIRECTION_NEUTRAL
    assert "перекупленности" in decision.reason


def test_oversold_cross_is_downgraded_to_neutral():
    """A downward crossover with oversold RSI - too late to sell."""
    decision = signal_service.analyse(make_candles(SELL_SERIES), {"rsi_oversold": 55})

    assert decision.direction == DIRECTION_NEUTRAL
    assert "перепроданности" in decision.reason


def test_reason_names_the_indicators():
    """A signal card must explain itself, not just say "buy"."""
    decision = signal_service.analyse(make_candles(BUY_SERIES))

    assert "EMA9" in decision.reason
    assert "EMA21" in decision.reason
    assert set(decision.indicators) >= {"ema9", "ema21", "rsi", "close"}


def test_custom_periods_are_used():
    decision = signal_service.analyse(
        make_candles(BUY_SERIES), {"ema_fast": 5, "ema_slow": 20}
    )
    if decision is not None:
        assert "ema5" in decision.indicators
        assert "ema20" in decision.indicators


def test_unclosed_candle_is_ignored():
    """An open candle can still change - deciding on it isn't allowed."""
    closed_only = signal_service.analyse(make_candles(BUY_SERIES))
    with_open_tail = signal_service.analyse(
        make_candles(BUY_SERIES + [999], open_tail=True)
    )

    assert closed_only is not None
    assert with_open_tail is not None
    # A huge open candle didn't change the decision: it was simply discarded.
    assert with_open_tail.candle_time == closed_only.candle_time
    assert with_open_tail.direction == closed_only.direction


# --- Writing to the database ---


@pytest_asyncio.fixture
async def rule_setup(session):
    exchange = Exchange(code="bybit", name="Bybit", sort_order=10)
    session.add(exchange)
    timeframe = Timeframe(code="1h", label="1 час", seconds=3600, sort_order=40)
    session.add(timeframe)
    await session.flush()

    await market_service.sync_markets(
        session, exchange, fakes.FakeAdapter(markets=[fakes.market("BTC/USDT", "BTC", "USDT")])
    )
    market = await market_service.get_market(session, exchange.id, "BTC/USDT")

    rule = SignalRule(
        name="EMA + RSI",
        timeframe_id=timeframe.id,
        market_id=market.id,
        config=signal_service.DEFAULT_CONFIG,
        evaluation_horizon_minutes=60,
    )
    session.add(rule)
    await session.flush()

    for index, price in enumerate(BUY_SERIES):
        value = Decimal(str(price))
        session.add(
            Candle(
                market_id=market.id,
                timeframe_id=timeframe.id,
                open_time=BASE_TIME + timedelta(hours=index),
                open=value, high=value, low=value, close=value, volume=Decimal(1),
                is_closed=True,
            )
        )
    await session.commit()

    return {"rule": rule, "market": market, "timeframe": timeframe}


async def test_signal_is_stored_with_explanation(session, rule_setup):
    signal = await signal_service.evaluate_rule(
        session, rule_setup["rule"], rule_setup["market"], rule_setup["timeframe"]
    )
    await session.commit()

    assert signal is not None
    assert signal.direction == DIRECTION_BUY
    assert signal.reason
    assert signal.indicators["rsi"] is not None


async def test_same_candle_does_not_produce_duplicate(session, rule_setup):
    """The engine may pass over a candle twice - there must be one signal."""
    first = await signal_service.evaluate_rule(
        session, rule_setup["rule"], rule_setup["market"], rule_setup["timeframe"]
    )
    await session.commit()
    second = await signal_service.evaluate_rule(
        session, rule_setup["rule"], rule_setup["market"], rule_setup["timeframe"]
    )
    await session.commit()

    assert first is not None
    assert second is None
    assert len((await session.execute(select(Signal))).scalars().all()) == 1


async def test_recent_signals_include_pair_and_timeframe(session, rule_setup):
    await signal_service.evaluate_rule(
        session, rule_setup["rule"], rule_setup["market"], rule_setup["timeframe"]
    )
    await session.commit()

    rows = await signal_service.recent_signals(session)

    assert len(rows) == 1
    _signal, symbol, timeframe_code = rows[0]
    assert symbol == "BTC/USDT"
    assert timeframe_code == "1h"


# --- Scoring the outcome ---


async def test_outcome_marks_successful_buy(session, rule_setup):
    signal = await signal_service.evaluate_rule(
        session, rule_setup["rule"], rule_setup["market"], rule_setup["timeframe"]
    )
    session.add(
        Candle(
            market_id=rule_setup["market"].id,
            timeframe_id=rule_setup["timeframe"].id,
            open_time=signal.candle_time + timedelta(hours=2),
            open=Decimal(200), high=Decimal(200), low=Decimal(200),
            close=Decimal(200), volume=Decimal(1), is_closed=True,
        )
    )
    await session.commit()

    evaluated = await signal_service.evaluate_outcomes(session)
    await session.commit()

    assert evaluated == 1
    outcome = (await session.execute(select(SignalOutcome))).scalar_one()
    assert outcome.is_success
    assert outcome.pnl_pct > 0


async def test_outcome_marks_failed_buy(session, rule_setup):
    signal = await signal_service.evaluate_rule(
        session, rule_setup["rule"], rule_setup["market"], rule_setup["timeframe"]
    )
    session.add(
        Candle(
            market_id=rule_setup["market"].id,
            timeframe_id=rule_setup["timeframe"].id,
            open_time=signal.candle_time + timedelta(hours=2),
            open=Decimal(10), high=Decimal(10), low=Decimal(10),
            close=Decimal(10), volume=Decimal(1), is_closed=True,
        )
    )
    await session.commit()

    await signal_service.evaluate_outcomes(session)
    await session.commit()

    outcome = (await session.execute(select(SignalOutcome))).scalar_one()
    assert not outcome.is_success
    assert outcome.pnl_pct < 0


async def test_outcome_waits_for_horizon(session, rule_setup):
    """A signal whose horizon hasn't passed yet isn't scored."""
    rule_setup["rule"].evaluation_horizon_minutes = 60 * 24 * 365
    await signal_service.evaluate_rule(
        session, rule_setup["rule"], rule_setup["market"], rule_setup["timeframe"]
    )
    await session.commit()

    assert await signal_service.evaluate_outcomes(session) == 0


async def test_outcome_is_written_once(session, rule_setup):
    signal = await signal_service.evaluate_rule(
        session, rule_setup["rule"], rule_setup["market"], rule_setup["timeframe"]
    )
    session.add(
        Candle(
            market_id=rule_setup["market"].id,
            timeframe_id=rule_setup["timeframe"].id,
            open_time=signal.candle_time + timedelta(hours=2),
            open=Decimal(200), high=Decimal(200), low=Decimal(200),
            close=Decimal(200), volume=Decimal(1), is_closed=True,
        )
    )
    await session.commit()

    await signal_service.evaluate_outcomes(session)
    await session.commit()
    assert await signal_service.evaluate_outcomes(session) == 0


async def test_accuracy_counts_only_evaluated(session, rule_setup):
    empty = await signal_service.accuracy(session)
    assert empty["total"] == 0
    assert empty["win_rate"] is None

    signal = await signal_service.evaluate_rule(
        session, rule_setup["rule"], rule_setup["market"], rule_setup["timeframe"]
    )
    session.add(
        SignalOutcome(
            signal_id=signal.id,
            horizon_minutes=60,
            price_after=Decimal(200),
            pnl_pct=Decimal(10),
            is_success=True,
        )
    )
    await session.commit()

    stats = await signal_service.accuracy(session)
    assert stats["total"] == 1
    assert stats["wins"] == 1
    assert stats["win_rate"] == 100.0
    assert stats["avg_pnl_pct"] == 10.0


# --- Neutral verdict ---


async def test_neutral_verdict_is_stored_too(session, rule_setup):
    """"There was a crossover, but entering isn't worth it" is a result too.

    Previously such a verdict was silently discarded, and the user saw silence where the
    engine had actually thought it over and decided not to enter.
    """
    rule = rule_setup["rule"]
    rule.config = dict(signal_service.DEFAULT_CONFIG, rsi_overbought=50)
    await session.flush()

    signal = await signal_service.evaluate_rule(
        session, rule, rule_setup["market"], rule_setup["timeframe"]
    )
    await session.commit()

    assert signal is not None
    assert signal.direction == DIRECTION_NEUTRAL
    assert "перекупленности" in signal.reason


async def test_neutral_verdict_stays_out_of_accuracy(session, rule_setup):
    """It says nothing about direction - so it can't "come true" either."""
    rule = rule_setup["rule"]
    rule.config = dict(signal_service.DEFAULT_CONFIG, rsi_overbought=50)
    rule.evaluation_horizon_minutes = 60
    await session.flush()

    signal = await signal_service.evaluate_rule(
        session, rule, rule_setup["market"], rule_setup["timeframe"]
    )
    signal.candle_time = datetime.now(timezone.utc) - timedelta(hours=3)
    await session.flush()

    session.add(
        Candle(
            market_id=rule_setup["market"].id,
            timeframe_id=rule_setup["timeframe"].id,
            open_time=datetime.now(timezone.utc) - timedelta(hours=1),
            open=Decimal(100), high=Decimal(100), low=Decimal(100),
            close=Decimal(100), volume=Decimal(1), is_closed=True,
        )
    )
    await session.commit()

    assert await signal_service.evaluate_outcomes(session) == 0
    stats = await signal_service.accuracy(session)
    assert stats["total"] == 0
