"""Технические сигналы: расчёт, выдача и статистика точности.

Правило по умолчанию — пересечение EMA с фильтром по RSI. Набор
индикаторов расширяется через config правила, без миграции схемы.

Каждый сигнал обязан объяснять себя: в карточке пользователь видит не
«buy», а причину и значения индикаторов на момент срабатывания.
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

# Сколько свечей нужно, чтобы индикаторы вышли на осмысленные значения.
MIN_CANDLES = 60


@dataclass(frozen=True)
class SignalDecision:
    direction: str
    reason: str
    indicators: dict
    price: Decimal
    candle_time: datetime


def analyse(candles: list[Candle], config: dict | None = None) -> SignalDecision | None:
    """Решение по последней закрытой свече.

    Чистая функция: ни базы, ни сети — поэтому её легко проверить на
    придуманных рядах, а движок и график считают одно и то же.
    """
    settings = {**DEFAULT_CONFIG, **(config or {})}

    # Последняя свеча ещё формируется: решать по ней — значит выдавать
    # сигнал, который исчезнет, если цена вернётся до закрытия периода.
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


# --- Работа с базой ---


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
    """Посчитать правило по одной паре и записать сигнал, если он новый.

    Нейтральный вердикт записывается наравне с покупкой и продажей: он
    возникает не на каждой свече, а только когда пересечение EMA было, но
    RSI против входа. Это самое содержательное объяснение, какое выдаёт
    движок, и выбрасывать его — значит показывать пользователю тишину там,
    где система на самом деле подумала и решила не входить.
    """
    candles = await candle_service.stored_candles(session, market, timeframe, limit=300)
    decision = analyse(candles, rule.config)
    if decision is None:
        return None

    if await _already_emitted(session, rule.id, market.id, decision.candle_time):
        # Тот же сигнал на той же свече — движок мог пройти по ней дважды.
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
    """Последние сигналы вместе с парой и таймфреймом."""
    query = (
        select(Signal, Market.symbol, Timeframe.code)
        .join(Market, Market.id == Signal.market_id)
        .join(Timeframe, Timeframe.id == Signal.timeframe_id)
        .join(SignalRule, SignalRule.id == Signal.rule_id)
        .order_by(Signal.created_at.desc())
        .limit(limit)
    )
    if user_id is not None:
        # Правила бывают общие (user_id пуст) и личные.
        query = query.where(
            (SignalRule.user_id == user_id) | (SignalRule.user_id.is_(None))
        )

    result = await session.execute(query)
    return [(signal, symbol, code) for signal, symbol, code in result]


# --- Оценка того, «сыграл» ли сигнал ---


async def evaluate_outcomes(session: AsyncSession) -> int:
    """Сверить старые сигналы с ценой по истечении горизонта.

    Это и есть материал для статистики точности: без честной оценки
    «сколько раз сработало» карточка сигнала — просто мнение.
    """
    now = datetime.now(timezone.utc)

    pending = await session.execute(
        select(Signal, SignalRule.evaluation_horizon_minutes)
        .join(SignalRule, SignalRule.id == Signal.rule_id)
        .outerjoin(SignalOutcome, SignalOutcome.signal_id == Signal.id)
        # Нейтральный вердикт ничего не утверждает о направлении, поэтому
        # и «сбыться» не может: в статистику точности он не идёт. Отсеиваем
        # его запросом, а не в цикле, иначе такие сигналы вечно занимали бы
        # окно выборки и вытесняли те, которые надо оценить.
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
            # Свечи на момент горизонта ещё нет — оценивать нечем.
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
    """Доля сигналов, ушедших в свою сторону."""
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
    """Цена первой свечи на момент горизонта или позже.

    Брать последнюю свечу до горизонта нельзя: если данных за нужный
    период нет, ею окажется свеча самого сигнала, и в статистику попадёт
    выдуманный результат «изменение 0%».
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
