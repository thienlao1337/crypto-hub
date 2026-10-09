"""Alerts: condition checks, triggering and delivery.

A condition is checked against the quote snapshot and, where needed, candles. The
triggering decision is separate from writing to the database, so it can be tested on
made-up data.
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

# Timeframe used for RSI and period change in alerts.
ALERT_TIMEFRAME = "5m"


class AlertError(Exception):
    """An incorrectly configured alert."""


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
    """Check a single condition. Pure function, no database."""
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

    logger.warning("Unknown alert type: %s", type_code)
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
        # Not enough history - stay silent rather than trigger at random.
        return None

    base = earlier[-1].close
    if base <= 0:
        return None

    change = (price - base) / base * Decimal(100)
    if abs(change) < abs(threshold):
        return None
    # The sign of the threshold sets the direction: -5 means "dropped by 5%".
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


# --- Database operations ---


def is_ready(alert: Alert, *, now: datetime | None = None) -> bool:
    """Whether the alert is allowed to fire right now."""
    now = now or datetime.now(timezone.utc)

    if not alert.is_active:
        return False
    if alert.expires_at is not None and _as_utc(alert.expires_at) < now:
        return False
    if alert.trigger_limit is not None and alert.trigger_count >= alert.trigger_limit:
        return False
    if alert.last_triggered_at is not None:
        # Cooldown after triggering: without it a "price above X" alert would
        # fire on every check while the price stays above the level.
        elapsed = now - _as_utc(alert.last_triggered_at)
        if elapsed < timedelta(seconds=alert.cooldown_seconds):
            return False
    return True


async def evaluate_all(session: AsyncSession) -> list[AlertTrigger]:
    """Check all active alerts and record triggers."""
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
            logger.exception("Could not check alert %s", alert.id)
            continue

        if hit is None:
            continue

        trigger = await fire(session, alert, hit)
        fired.append(trigger)

    await session.flush()
    return fired


async def fire(session: AsyncSession, alert: Alert, hit: AlertHit) -> AlertTrigger:
    """Record a trigger and queue a notification in the feed."""
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

    # The notification needs the trigger id so that sending can later mark
    # delivery on exactly this row.
    await session.flush()

    notification = await notification_service.dispatch(
        session,
        user_id=alert.user_id,
        kind=notification_service.KIND_ALERT,
        title="Сработал алерт",
        body=hit.message,
        payload={"alert_id": alert.id, "trigger_id": trigger.id},
        web=alert.notify_web,
        telegram=alert.notify_telegram,
    )
    if notification is not None:
        trigger.delivered_web = notification.show_web

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
    trigger_limit: int | None = None,
    expires_at: datetime | None = None,
) -> Alert:
    alert_type = await _alert_type(session, type_code)
    validate_params(type_code, params)
    validate_limits(trigger_limit, expires_at)

    alert = Alert(
        user_id=user.id,
        alert_type_id=alert_type.id,
        market_id=market_id,
        params=params,
        cooldown_seconds=max(60, cooldown_seconds),
        notify_web=notify_web,
        notify_telegram=notify_telegram,
        trigger_limit=trigger_limit,
        expires_at=expires_at,
    )
    session.add(alert)
    await session.flush()
    return alert


async def update_alert(
    session: AsyncSession,
    alert: Alert,
    *,
    market_id: int,
    type_code: str,
    params: dict,
    cooldown_seconds: int = 3600,
    notify_web: bool = True,
    notify_telegram: bool = True,
    trigger_limit: int | None = None,
    expires_at: datetime | None = None,
) -> Alert:
    """Edit an existing alert.

    The trigger counter is kept: it counts over the alert's lifetime and matches the
    history rows. That's why the trigger limit is also counted from the start of the
    alert's life, not from the last edit - the UI shows the current count next to it so
    this isn't a surprise.

    The cooldown after the last trigger, however, is cleared when the condition changes -
    otherwise the new condition would stay silent until the end of the cooldown
    assigned to the old one.
    """
    alert_type = await _alert_type(session, type_code)
    validate_params(type_code, params)
    validate_limits(trigger_limit, expires_at)

    condition_changed = (
        alert.alert_type_id != alert_type.id
        or alert.market_id != market_id
        or (alert.params or {}) != params
    )

    alert.alert_type_id = alert_type.id
    alert.market_id = market_id
    alert.params = params
    alert.cooldown_seconds = max(60, cooldown_seconds)
    alert.notify_web = notify_web
    alert.notify_telegram = notify_telegram
    alert.trigger_limit = trigger_limit
    alert.expires_at = expires_at

    if condition_changed:
        alert.last_triggered_at = None

    await session.flush()
    return alert


async def recent_triggers(
    session: AsyncSession, alert: Alert, *, limit: int = 20
) -> list[AlertTrigger]:
    result = await session.execute(
        select(AlertTrigger)
        .where(AlertTrigger.alert_id == alert.id)
        .order_by(AlertTrigger.triggered_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


def validate_limits(trigger_limit: int | None, expires_at: datetime | None) -> None:
    """Validate the alert's lifetime limits.

    An expiry in the past must not be accepted: the alert would silently never fire, and
    the user would look for the cause in the condition.
    """
    if trigger_limit is not None and trigger_limit < 1:
        raise AlertError("Количество срабатываний — целое число от 1.")
    if expires_at is not None and _as_utc(expires_at) <= datetime.now(timezone.utc):
        raise AlertError("Срок действия уже истёк — укажите будущее время.")


def validate_params(type_code: str, params: dict) -> None:
    """Validate input before saving so the alert doesn't stay silent because of a typo."""
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
    """A number in the message without trailing zeros.

    Numeric(36, 18) returns 79761.900000000000000000, and in a notification text that
    looks like a glitch, not a price.
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
