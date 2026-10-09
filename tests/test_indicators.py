"""Indicator tests on values computed by hand.

That's the whole point: check the implementation against the formula, not against
itself.
"""

import math

import pandas as pd
import pytest

from app.services import indicators


def series(*values) -> pd.Series:
    return pd.Series([float(v) for v in values], dtype="float64")


# --- Moving averages ---


def test_sma_matches_hand_calculation():
    result = indicators.sma(series(1, 2, 3, 4, 5), 3)

    assert math.isnan(result.iloc[0])
    assert math.isnan(result.iloc[1])
    assert result.iloc[2] == 2.0  # (1+2+3)/3
    assert result.iloc[3] == 3.0
    assert result.iloc[4] == 4.0


def test_ema_follows_recursive_formula():
    """EMA(t) = price * k + EMA(t-1) * (1 - k), k = 2 / (period + 1)."""
    prices = series(10, 20, 30)
    result = indicators.ema(prices, 2)
    k = 2 / (2 + 1)

    expected_1 = 10.0
    expected_2 = 20 * k + expected_1 * (1 - k)
    expected_3 = 30 * k + expected_2 * (1 - k)

    assert result.iloc[0] == pytest.approx(expected_1)
    assert result.iloc[1] == pytest.approx(expected_2)
    assert result.iloc[2] == pytest.approx(expected_3)


def test_ema_reacts_faster_than_sma():
    """The comparison has to be made at the start of the move.

    After a full window the simple average fully catches up with the price and the
    difference disappears - EMA's advantage is in the first bars after the shift.
    """
    prices = series(*([10] * 10 + [20] * 2))

    assert indicators.ema(prices, 5).iloc[-1] > indicators.sma(prices, 5).iloc[-1]


@pytest.mark.parametrize("period", [0, -1])
def test_invalid_period_rejected(period):
    with pytest.raises(ValueError):
        indicators.sma(series(1, 2, 3), period)


# --- RSI ---


def test_rsi_is_100_when_price_only_rises():
    """Without a single drop there's no resistance - RSI hits 100."""
    prices = series(*range(1, 30))
    result = indicators.rsi(prices, 14)

    assert result.iloc[-1] == pytest.approx(100.0)


def test_rsi_is_zero_when_price_only_falls():
    prices = series(*range(30, 1, -1))
    result = indicators.rsi(prices, 14)

    assert result.iloc[-1] == pytest.approx(0.0)


def test_rsi_stays_in_range():
    prices = series(44, 44.3, 44.1, 44.6, 43.4, 44.3, 44.8, 45.1, 45.4, 45.4,
                    46.2, 46.3, 46.3, 46, 46, 46.4, 46.2, 45.6, 46.2, 46.2)
    result = indicators.rsi(prices, 14).dropna()

    assert not result.empty
    assert result.between(0, 100).all()


def test_rsi_needs_enough_history():
    result = indicators.rsi(series(1, 2, 3), 14)
    assert result.isna().all(), "на трёх свечах RSI(14) считать нечего"


# --- MACD ---


def test_macd_is_difference_of_emas():
    prices = series(*range(1, 60))
    result = indicators.macd(prices, 12, 26, 9)

    expected = indicators.ema(prices, 12) - indicators.ema(prices, 26)
    assert result.macd.iloc[-1] == pytest.approx(expected.iloc[-1])
    assert result.histogram.iloc[-1] == pytest.approx(
        result.macd.iloc[-1] - result.signal.iloc[-1]
    )


def test_macd_rejects_fast_slower_than_slow():
    with pytest.raises(ValueError):
        indicators.macd(series(*range(1, 40)), fast=26, slow=12)


# --- Bollinger Bands ---


def test_bollinger_collapses_on_flat_price():
    """Without fluctuations the deviation is zero and the bands collapse onto the average."""
    prices = series(*([100] * 25))
    result = indicators.bollinger(prices, 20)

    assert result.middle.iloc[-1] == pytest.approx(100.0)
    assert result.upper.iloc[-1] == pytest.approx(100.0)
    assert result.lower.iloc[-1] == pytest.approx(100.0)


def test_bollinger_bands_are_symmetric():
    prices = series(*[100 + (i % 7) for i in range(40)])
    result = indicators.bollinger(prices, 20, 2.0)

    middle = result.middle.iloc[-1]
    assert result.upper.iloc[-1] - middle == pytest.approx(middle - result.lower.iloc[-1])


# --- EMA crossover ---


def test_ema_cross_reports_event_not_state():
    """The signal must appear at the moment of the crossover, not persist afterwards."""
    prices = series(*([10] * 15 + [30] * 15))
    crosses = indicators.ema_cross(prices, 3, 10)

    up_points = [i for i, value in enumerate(crosses) if value == 1]
    assert len(up_points) == 1, "рост должен дать ровно одно пересечение вверх"
    # The jump starts on the fifteenth candle - there's nothing to cross before that.
    assert up_points[0] >= 15


def test_ema_cross_detects_both_directions():
    prices = series(*([10] * 15 + [40] * 15 + [5] * 15))
    crosses = indicators.ema_cross(prices, 3, 10)

    assert 1 in list(crosses)
    assert -1 in list(crosses)


def test_ema_cross_is_quiet_without_movement():
    crosses = indicators.ema_cross(series(*([10] * 30)), 3, 10)
    assert set(crosses) == {0}


# --- Helpers ---


def test_to_series_accepts_decimal():
    from decimal import Decimal

    result = indicators.to_series([Decimal("1.5"), Decimal("2.5")])
    assert list(result) == [1.5, 2.5]


def test_last_value_skips_missing_tail():
    assert indicators.last_value(series(1, 2, 3)) == 3.0
    assert indicators.last_value(pd.Series([float("nan")])) is None
    assert indicators.last_value(pd.Series([], dtype="float64")) is None
