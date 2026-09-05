"""Технические индикаторы.

Чистые функции над рядом цен: ни базы, ни сети — поэтому их легко
проверять на эталонных значениях, а движок сигналов может считать то же
самое, что рисует график.

Здесь сознательно используется float, а не Decimal. Индикаторы — это
статистика по ценам, а не деньги: скользящая средняя не обязана сходиться
до последнего знака, зато вычисления идут в разы быстрее. Decimal
остаётся там, где считаются балансы и стоимость портфеля.

Реализовано своими руками, а не библиотекой: это около сотни строк,
которые не отстают от новых версий pandas и покрыты тестами на понятных
значениях.
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
    """Простая скользящая средняя."""
    _check_period(period)
    return values.rolling(window=period, min_periods=period).mean()


def ema(values: pd.Series, period: int) -> pd.Series:
    """Экспоненциальная скользящая средняя.

    adjust=False — рекурсивная форма, та же, что у торговых терминалов:
    EMA(t) = price(t) * k + EMA(t-1) * (1 - k), где k = 2 / (period + 1).
    При adjust=True значения в начале ряда заметно расходятся с тем, что
    пользователь видит в TradingView.
    """
    _check_period(period)
    return values.ewm(span=period, adjust=False).mean()


def rsi(values: pd.Series, period: int = 14) -> pd.Series:
    """Индекс относительной силы по Уайлдеру.

    Сглаживание — экспоненциальное с alpha = 1 / period, как в оригинале,
    а не простое среднее: иначе значения разойдутся с биржевыми графиками.
    """
    _check_period(period)

    delta = values.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    # Ряд без единого падения даёт нулевой знаменатель — это не ошибка,
    # а честные 100: сила покупателей не встретила сопротивления.
    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    return result.where(avg_loss != 0, 100.0).where(avg_gain.notna(), float("nan"))


def macd(
    values: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MacdResult:
    """Схождение и расхождение скользящих средних."""
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
    """Полосы Боллинджера.

    Отклонение считается по всей выборке окна (ddof=0), как в торговых
    терминалах, а не по несмещённой оценке — иначе полосы окажутся чуть
    шире привычных.
    """
    _check_period(period)

    middle = sma(values, period)
    spread = values.rolling(window=period, min_periods=period).std(ddof=0) * deviations
    return BollingerResult(middle=middle, upper=middle + spread, lower=middle - spread)


def ema_cross(values: pd.Series, fast: int, slow: int) -> pd.Series:
    """Направление пересечения быстрой и медленной EMA.

    1 — быстрая пересекла медленную снизу вверх на этой свече,
    -1 — сверху вниз, 0 — пересечения не было. Именно событие, а не
    факт «быстрая выше медленной»: иначе сигнал повторялся бы на каждой
    свече, пока сохраняется расположение линий.
    """
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)

    above = fast_line > slow_line
    crossed_up = above & ~above.shift(1, fill_value=False)
    crossed_down = ~above & above.shift(1, fill_value=False)

    result = pd.Series(0, index=values.index, dtype="int64")
    result[crossed_up] = 1
    result[crossed_down] = -1
    # На первой свече сравнивать не с чем.
    if len(result):
        result.iloc[0] = 0
    return result


def to_series(closes) -> pd.Series:
    """Собрать ряд для расчётов из чего угодно числового (в том числе Decimal)."""
    return pd.Series([float(value) for value in closes], dtype="float64")


def last_value(series: pd.Series) -> float | None:
    """Последнее осмысленное значение ряда или None, если данных мало."""
    if series is None or series.empty:
        return None
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError("Период индикатора должен быть положительным.")
