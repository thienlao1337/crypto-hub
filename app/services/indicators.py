"""Technical indicators.

Pure functions over a price series: no database, no network - so they're easy to check
against reference values, and the signal engine can compute exactly what the chart
draws.

float is used here on purpose, not Decimal. Indicators are statistics over prices, not
money: a moving average doesn't have to match to the last digit, and the computation is
several times faster. Decimal stays where balances and portfolio value are calculated.

Implemented by hand rather than with a library: it's about a hundred lines that don't
lag behind new pandas versions and are covered by tests on clear values.
"""

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class MacdResult:
    macd: pd.Series
    signal: pd.Series
    histogram: pd.Series


@dataclass(frozen=True)
class BollingerResult:
    middle: pd.Series
    upper: pd.Series
    lower: pd.Series


def sma(values: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    _check_period(period)
    return values.rolling(window=period, min_periods=period).mean()


def ema(values: pd.Series, period: int) -> pd.Series:
    """Exponential moving average.

    adjust=False is the recursive form, the same one trading terminals use:
    EMA(t) = price(t) * k + EMA(t-1) * (1 - k), where k = 2 / (period + 1).
    With adjust=True the values at the start of the series noticeably differ from what
    the user sees in TradingView.
    """
    _check_period(period)
    return values.ewm(span=period, adjust=False).mean()


def rsi(values: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index.

    Smoothing is exponential with alpha = 1 / period, as in the original, not a simple
    average: otherwise the values diverge from exchange charts.
    """
    _check_period(period)

    delta = values.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    # A series with no drops at all gives a zero denominator - not an error but
    # an honest 100: buying pressure met no resistance.
    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    return result.where(avg_loss != 0, 100.0).where(avg_gain.notna(), float("nan"))


def macd(
    values: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MacdResult:
    """Moving Average Convergence Divergence."""
    _check_period(fast)
    _check_period(slow)
    _check_period(signal)
    if fast >= slow:
        raise ValueError("Быстрый период должен быть короче медленного.")

    macd_line = ema(values, fast) - ema(values, slow)
    signal_line = ema(macd_line, signal)
    return MacdResult(
        macd=macd_line,
        signal=signal_line,
        histogram=macd_line - signal_line,
    )


def bollinger(values: pd.Series, period: int = 20, deviations: float = 2.0) -> BollingerResult:
    """Bollinger Bands.

    Deviation is computed over the whole window population (ddof=0), as in trading
    terminals, not the unbiased estimate - otherwise the bands come out slightly wider
    than usual.
    """
    _check_period(period)

    middle = sma(values, period)
    spread = values.rolling(window=period, min_periods=period).std(ddof=0) * deviations
    return BollingerResult(middle=middle, upper=middle + spread, lower=middle - spread)


def ema_cross(values: pd.Series, fast: int, slow: int) -> pd.Series:
    """Direction of the crossover between the fast and slow EMA.

    1 - the fast one crossed the slow one upwards on this candle, -1 - downwards, 0 - no
    crossover. It's the event, not the fact "fast is above slow": otherwise the signal
    would repeat on every candle while the lines stay in that order.
    """
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)

    above = fast_line > slow_line
    crossed_up = above & ~above.shift(1, fill_value=False)
    crossed_down = ~above & above.shift(1, fill_value=False)

    result = pd.Series(0, index=values.index, dtype="int64")
    result[crossed_up] = 1
    result[crossed_down] = -1
    # On the first candle there's nothing to compare with.
    if len(result):
        result.iloc[0] = 0
    return result


def to_series(closes) -> pd.Series:
    """Build a series for calculations from anything numeric (Decimal included)."""
    return pd.Series([float(value) for value in closes], dtype="float64")


def last_value(series: pd.Series) -> float | None:
    """The last meaningful value of the series, or None if there's too little data."""
    if series is None or series.empty:
        return None
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError("Период индикатора должен быть положительным.")
