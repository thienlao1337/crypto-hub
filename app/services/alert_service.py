"""Алерты: проверка условий, срабатывание и доставка.

Условие проверяется по срезу котировок и, где нужно, по свечам. Решение
о срабатывании отделено от записи в базу — так его можно проверить на
придуманных данных.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Alert,
    AlertTrigger,
    AlertType,
    Candle,
    Market,
    MarketTicker,
    Timeframe,
    User,
)
from app.services import indicators, notification_service

logger = logging.getLogger(__name__)

TYPE_PRICE_ABOVE = "price_above"
TYPE_PRICE_BELOW = "price_below"
TYPE_PCT_CHANGE = "pct_change"
TYPE_RSI = "rsi"

# Таймфрейм, на котором считаются RSI и изменение за период для алертов.
ALERT_TIMEFRAME = "5m"


class AlertError(Exception):
    """Некорректно заданный алерт."""


@dataclass(frozen=True)
class AlertHit:
    message: str
    price: Decimal | None


def check(
    type_code: str,
    params: dict,
    *,
    price: Decimal | None,
    symbol: str,
    candles: list[Candle] | None = None,
) -> AlertHit | None:
    """Проверить одно условие. Чистая функция, без базы."""
    if type_code == TYPE_PRICE_ABOVE:
        level = _decimal(params.get("level"))
        if price is None or level is None or price <= level:
            return None
        return AlertHit(f"{symbol}: цена {_num(price)} поднялась выше {_num(level)}.", price)

    if type_code == TYPE_PRICE_BELOW:
        level = _decimal(params.get("level"))
        if price is None or level is None or price >= level:
            return None
        return AlertHit(f"{symbol}: цена {_num(price)} опустилась ниже {_num(level)}.", price)

    if type_code == TYPE_PCT_CHANGE:
        return _check_pct_change(params, price, symbol, candles)

    if type_code == TYPE_RSI:
        return _check_rsi(params, symbol, candles)

    logger.warning("Неизвестный тип алерта: %s", type_code)
    return None


def _check_pct_change(
    params: dict, price: Decimal | None, symbol: str, candles: list[Candle] | None
) -> AlertHit | None:
    threshold = _decimal(params.get("pct"))
    window = int(params.get("window_minutes") or 60)
    if price is None or threshold is None or not candles:
        return None

    since = datetime.now(timezone.utc) - timedelta(minutes=window)
    earlier = [c for c in candles if _as_utc(c.open_time) <= since]
    if not earlier:
        # Истории не хватает — молчим, а не выдаём срабатывание наугад.
        return None

    base = earlier[-1].close
    if base <= 0:
        return None

    change = (price - base) / base * Decimal(100)
    if abs(change) < abs(threshold):
        return None
    # Знак порога задаёт направление: -5 означает «упало на 5%».
    if threshold > 0 and change < 0:
        return None
    if threshold < 0 and change > 0:
        return None

    direction = "выросла" if change > 0 else "упала"
    return AlertHit(
        f"{symbol}: цена {direction} на {abs(change):.2f}% за {window} мин "
        f"(с {_num(base)} до {_num(price)}).",
        price,
    )


def _check_rsi(params: dict, symbol: str, candles: list[Candle] | None) -> AlertHit | None:
    threshold = _decimal(params.get("threshold"))
    period = int(params.get("period") or 14)
    direction = (params.get("direction") or "above").lower()
    if threshold is None or not candles or len(candles) < period + 1:
        return None

    closes = indicators.to_series([c.close for c in candles])
    value = indicators.last_value(indicators.rsi(closes, period))
    if value is None:
        return None

    crossed_up = direction == "above" and value >= float(threshold)
    crossed_down = direction == "below" and value <= float(threshold)
    if not (crossed_up or crossed_down):
        return None

    word = "поднялся выше" if crossed_up else "опустился ниже"
    return AlertHit(f"{symbol}: RSI({period}) {value:.1f} {word} {_num(threshold)}.", None)


# --- Работа с базой ---


def is_ready(alert: Alert, *, now: datetime | None = None) -> bool:
    """Можно ли алерту срабатывать прямо сейчас."""
    now = now or datetime.now(timezone.utc)

    if not alert.is_active:
        return False
    if alert.expires_at is not None and _as_utc(alert.expires_at) < now:
        return False
    if alert.trigger_limit is not None and alert.trigger_count >= alert.trigger_limit:
        return False
    if alert.last_triggered_at is not None:
        # Пауза после срабатывания: без неё алерт «цена выше X» звонил бы
        # на каждой проверке, пока цена держится выше уровня.
        elapsed = now - _as_utc(alert.last_triggered_at)
        if elapsed < timedelta(seconds=alert.cooldown_seconds):
            return False
    return True


async def evaluate_all(session: AsyncSession) -> list[AlertTrigger]:
    """Проверить все активные алерты и записать срабатывания."""
    rows = await session.execute(
        select(Alert, AlertType.code, Market.symbol)
        .join(AlertType, AlertType.id == Alert.alert_type_id)
        .join(Market, Market.id == Alert.market_id)
        .where(Alert.is_active.is_(True))
    )

    timeframe = await _alert_timeframe(session)
    fired: list[AlertTrigger] = []

    for alert, type_code, symbol in rows:
        if not is_ready(alert):
            continue

        ticker = await session.get(MarketTicker, alert.market_id)
        price = ticker.last if ticker else None

        candles = None
        if type_code in (TYPE_PCT_CHANGE, TYPE_RSI) and timeframe is not None:
            candles = await _recent_candles(session, alert.market_id, timeframe.id)

        try:
            hit = check(type_code, alert.params or {}, price=price, symbol=symbol, candles=candles)
        except Exception:
            logger.exception("Алерт %s не удалось проверить", alert.id)
            continue

        if hit is None:
            continue

        trigger = await fire(session, alert, hit)
        fired.append(trigger)

    await session.flush()
    return fired


async def fire(session: AsyncSession, alert: Alert, hit: AlertHit) -> AlertTrigger:
    """Записать срабатывание и поставить уведомление в ленту."""
    now = datetime.now(timezone.utc)

    trigger = AlertTrigger(
        alert_id=alert.id,
        price=hit.price,
        message=hit.message,
        delivered_web=False,
        delivered_telegram=False,
    )
    session.add(trigger)

    alert.last_triggered_at = now
    alert.trigger_count += 1

    if alert.notify_web:
        await notification_service.push(
            session,
            user_id=alert.user_id,
            kind=notification_service.KIND_ALERT,
            title="Сработал алерт",
            body=hit.message,
            payload={"alert_id": alert.id},
        )
        trigger.delivered_web = True

    await session.flush()
    return trigger


async def list_alerts(session: AsyncSession, user: User) -> list[tuple[Alert, str, str]]:
    result = await session.execute(
        select(Alert, AlertType.name, Market.symbol)
        .join(AlertType, AlertType.id == Alert.alert_type_id)
        .join(Market, Market.id == Alert.market_id)
        .where(Alert.user_id == user.id)
        .order_by(Alert.created_at.desc())
    )
    return [(alert, type_name, symbol) for alert, type_name, symbol in result]


async def get_alert(session: AsyncSession, user: User, alert_id: int) -> Alert:
    result = await session.execute(
        select(Alert).where(Alert.id == alert_id, Alert.user_id == user.id)
    )
    alert = result.scalar_one_or_none()
    if alert is None:
        raise AlertError("Алерт не найден.")
    return alert


async def create_alert(
    session: AsyncSession,
    user: User,
    *,
    market_id: int,
    type_code: str,
    params: dict,
    cooldown_seconds: int = 3600,
    notify_web: bool = True,
    notify_telegram: bool = True,
) -> Alert:
    alert_type = await _alert_type(session, type_code)
    validate_params(type_code, params)

    alert = Alert(
        user_id=user.id,
        alert_type_id=alert_type.id,
        market_id=market_id,
        params=params,
        cooldown_seconds=max(60, cooldown_seconds),
        notify_web=notify_web,
        notify_telegram=notify_telegram,
    )
    session.add(alert)
    await session.flush()
    return alert


def validate_params(type_code: str, params: dict) -> None:
    """Проверить ввод до записи, чтобы алерт не молчал из-за опечатки."""
    if type_code in (TYPE_PRICE_ABOVE, TYPE_PRICE_BELOW):
        level = _decimal(params.get("level"))
        if level is None or level <= 0:
            raise AlertError("Укажите положительный уровень цены.")

    elif type_code == TYPE_PCT_CHANGE:
        pct = _decimal(params.get("pct"))
        if pct is None or pct == 0:
            raise AlertError("Укажите изменение в процентах, отличное от нуля.")
        window = int(params.get("window_minutes") or 0)
        if window < 1:
            raise AlertError("Укажите период в минутах.")

    elif type_code == TYPE_RSI:
        threshold = _decimal(params.get("threshold"))
        if threshold is None or not (0 < threshold < 100):
            raise AlertError("Порог RSI должен быть между 0 и 100.")
        if (params.get("direction") or "above") not in ("above", "below"):
            raise AlertError("Направление RSI должно быть above или below.")

    else:
        raise AlertError("Неизвестный тип алерта.")


async def _alert_type(session: AsyncSession, code: str) -> AlertType:
    result = await session.execute(select(AlertType).where(AlertType.code == code))
    alert_type = result.scalar_one_or_none()
    if alert_type is None or not alert_type.is_active:
        raise AlertError("Такой тип алерта недоступен.")
    return alert_type


async def _alert_timeframe(session: AsyncSession) -> Timeframe | None:
    result = await session.execute(select(Timeframe).where(Timeframe.code == ALERT_TIMEFRAME))
    return result.scalar_one_or_none()


async def _recent_candles(
    session: AsyncSession, market_id: int, timeframe_id: int, limit: int = 200
) -> list[Candle]:
    result = await session.execute(
        select(Candle)
        .where(Candle.market_id == market_id, Candle.timeframe_id == timeframe_id)
        .order_by(Candle.open_time.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


def _num(value: Decimal | None) -> str:
    """Число в сообщении без хвоста нулей.

    Numeric(36, 18) возвращает 79761.900000000000000000, и в тексте
    уведомления это выглядит как сбой, а не как цена.
    """
    if value is None:
        return "—"
    text = format(value.normalize(), "f")
    return text


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
