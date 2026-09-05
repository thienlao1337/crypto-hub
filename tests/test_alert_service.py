from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models import (
    Alert,
    AlertTrigger,
    AlertType,
    Candle,
    Exchange,
    MarketTicker,
    Notification,
    Timeframe,
)
from app.services import alert_service, market_service, user_service
from tests import fakes

NOW = datetime.now(timezone.utc)

ALERT_TYPES = [
    ("price_above", "Цена выше уровня"),
    ("price_below", "Цена ниже уровня"),
    ("pct_change", "Изменение в процентах"),
    ("rsi", "Уровень RSI"),
]


@pytest_asyncio.fixture
async def setup(session):
    exchange = Exchange(code="bybit", name="Bybit", sort_order=10)
    session.add(exchange)
    session.add(Timeframe(code="5m", label="5 минут", seconds=300, sort_order=20))
    for order, (code, name) in enumerate(ALERT_TYPES):
        session.add(AlertType(code=code, name=name, sort_order=order))
    await session.flush()

    await market_service.sync_markets(
        session, exchange, fakes.FakeAdapter(markets=[fakes.market("BTC/USDT", "BTC", "USDT")])
    )
    market = await market_service.get_market(session, exchange.id, "BTC/USDT")

    session.add(MarketTicker(market_id=market.id, last=Decimal("80000")))
    user = await user_service.create_user(
        session, email="trader@example.com", password="trader-password-1"
    )
    await session.commit()

    return {"user": user, "market": market, "exchange": exchange}


def add_candles(session, market_id, timeframe_id, prices, *, minutes_step: int = 5):
    start = NOW - timedelta(minutes=minutes_step * len(prices))
    for index, price in enumerate(prices):
        value = Decimal(str(price))
        session.add(
            Candle(
                market_id=market_id,
                timeframe_id=timeframe_id,
                open_time=start + timedelta(minutes=minutes_step * index),
                open=value, high=value, low=value, close=value, volume=Decimal(1),
                is_closed=True,
            )
        )


# --- Условия ---


def test_price_above_triggers_only_when_exceeded():
    params = {"level": "79000"}

    hit = alert_service.check("price_above", params, price=Decimal("80000"), symbol="BTC/USDT")
    assert hit is not None
    assert "выше" in hit.message

    assert alert_service.check(
        "price_above", params, price=Decimal("78000"), symbol="BTC/USDT"
    ) is None


def test_message_shows_readable_numbers():
    """Цена в тексте не должна тянуть хвост нулей.

    Numeric(36, 18) отдаёт 79761.900000000000000000, и в уведомлении это
    выглядит как сбой, а не как цена.
    """
    hit = alert_service.check(
        "price_above",
        {"level": "1"},
        price=Decimal("79761.900000000000000000"),
        symbol="BTC/USDT",
    )

    assert "79761.9" in hit.message
    assert "79761.900000" not in hit.message


def test_price_below_triggers_only_when_dropped():
    params = {"level": "79000"}

    assert alert_service.check(
        "price_below", params, price=Decimal("78000"), symbol="BTC/USDT"
    ) is not None
    assert alert_service.check(
        "price_below", params, price=Decimal("80000"), symbol="BTC/USDT"
    ) is None


def test_price_alert_is_quiet_without_price():
    assert alert_service.check(
        "price_above", {"level": "1"}, price=None, symbol="BTC/USDT"
    ) is None


def test_pct_change_needs_history():
    """Без свечей за период молчим, а не выдаём срабатывание наугад."""
    assert alert_service.check(
        "pct_change", {"pct": 5, "window_minutes": 60},
        price=Decimal("80000"), symbol="BTC/USDT", candles=[],
    ) is None


def test_pct_change_direction_matters():
    old = NOW - timedelta(hours=3)
    candles = [
        Candle(market_id=1, timeframe_id=1, open_time=old,
               open=Decimal(100), high=Decimal(100), low=Decimal(100),
               close=Decimal(100), volume=Decimal(1), is_closed=True)
    ]

    grew = alert_service.check(
        "pct_change", {"pct": 5, "window_minutes": 60},
        price=Decimal(110), symbol="BTC/USDT", candles=candles,
    )
    assert grew is not None and "выросла" in grew.message

    # Порог на рост не должен срабатывать на падении.
    assert alert_service.check(
        "pct_change", {"pct": 5, "window_minutes": 60},
        price=Decimal(90), symbol="BTC/USDT", candles=candles,
    ) is None

    fell = alert_service.check(
        "pct_change", {"pct": -5, "window_minutes": 60},
        price=Decimal(90), symbol="BTC/USDT", candles=candles,
    )
    assert fell is not None and "упала" in fell.message


def test_rsi_alert_above_and_below():
    rising = [
        Candle(market_id=1, timeframe_id=1,
               open_time=NOW - timedelta(minutes=5 * (40 - i)),
               open=Decimal(100 + i), high=Decimal(100 + i), low=Decimal(100 + i),
               close=Decimal(100 + i), volume=Decimal(1), is_closed=True)
        for i in range(40)
    ]

    hit = alert_service.check(
        "rsi", {"threshold": 70, "direction": "above", "period": 14},
        price=None, symbol="BTC/USDT", candles=rising,
    )
    assert hit is not None and "RSI" in hit.message

    assert alert_service.check(
        "rsi", {"threshold": 30, "direction": "below", "period": 14},
        price=None, symbol="BTC/USDT", candles=rising,
    ) is None


def test_unknown_type_is_ignored():
    assert alert_service.check("что-то своё", {}, price=Decimal(1), symbol="X") is None


# --- Готовность к срабатыванию ---


def test_cooldown_blocks_repeat():
    alert = Alert(
        user_id=1, alert_type_id=1, market_id=1, params={},
        cooldown_seconds=3600, last_triggered_at=NOW - timedelta(minutes=10),
        is_active=True, trigger_count=1,
    )
    assert not alert_service.is_ready(alert, now=NOW)

    alert.last_triggered_at = NOW - timedelta(hours=2)
    assert alert_service.is_ready(alert, now=NOW)


def test_expired_alert_does_not_fire():
    alert = Alert(
        user_id=1, alert_type_id=1, market_id=1, params={},
        cooldown_seconds=60, is_active=True, expires_at=NOW - timedelta(minutes=1),
    )
    assert not alert_service.is_ready(alert, now=NOW)


def test_trigger_limit_is_respected():
    alert = Alert(
        user_id=1, alert_type_id=1, market_id=1, params={},
        cooldown_seconds=60, is_active=True, trigger_limit=2, trigger_count=2,
    )
    assert not alert_service.is_ready(alert, now=NOW)

    alert.trigger_count = 1
    assert alert_service.is_ready(alert, now=NOW)


def test_inactive_alert_does_not_fire():
    alert = Alert(user_id=1, alert_type_id=1, market_id=1, params={}, is_active=False)
    assert not alert_service.is_ready(alert, now=NOW)


# --- Проверка ввода ---


@pytest.mark.parametrize(
    ("type_code", "params"),
    [
        ("price_above", {"level": "0"}),
        ("price_above", {"level": "не число"}),
        ("pct_change", {"pct": 0, "window_minutes": 60}),
        ("pct_change", {"pct": 5, "window_minutes": 0}),
        ("rsi", {"threshold": 150}),
        ("rsi", {"threshold": 70, "direction": "вбок"}),
        ("выдумка", {}),
    ],
)
def test_bad_params_rejected(type_code, params):
    with pytest.raises(alert_service.AlertError):
        alert_service.validate_params(type_code, params)


def test_good_params_accepted():
    alert_service.validate_params("price_above", {"level": "70000"})
    alert_service.validate_params("pct_change", {"pct": -5, "window_minutes": 60})
    alert_service.validate_params("rsi", {"threshold": 70, "direction": "above"})


# --- Полный цикл ---


async def test_alert_fires_and_creates_notification(session, setup):
    await alert_service.create_alert(
        session, setup["user"],
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "79000"},
    )
    await session.commit()

    fired = await alert_service.evaluate_all(session)
    await session.commit()

    assert len(fired) == 1
    triggers = (await session.execute(select(AlertTrigger))).scalars().all()
    assert triggers[0].delivered_web

    notifications = (await session.execute(select(Notification))).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].kind == "alert"


async def test_alert_does_not_repeat_within_cooldown(session, setup):
    await alert_service.create_alert(
        session, setup["user"],
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "79000"},
        cooldown_seconds=3600,
    )
    await session.commit()

    first = await alert_service.evaluate_all(session)
    await session.commit()
    second = await alert_service.evaluate_all(session)
    await session.commit()

    assert len(first) == 1
    assert second == [], "пауза после срабатывания должна молчать"


async def test_alert_below_level_stays_silent(session, setup):
    await alert_service.create_alert(
        session, setup["user"],
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "90000"},
    )
    await session.commit()

    assert await alert_service.evaluate_all(session) == []


async def test_other_users_alert_is_not_accessible(session, setup):
    alert = await alert_service.create_alert(
        session, setup["user"],
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "79000"},
    )
    stranger = await user_service.create_user(
        session, email="stranger@example.com", password="stranger-password-1"
    )
    await session.commit()

    with pytest.raises(alert_service.AlertError):
        await alert_service.get_alert(session, stranger, alert.id)

    assert (await alert_service.get_alert(session, setup["user"], alert.id)).id == alert.id


async def test_cooldown_has_lower_bound(session, setup):
    """Слишком короткая пауза превратила бы алерт в спам."""
    alert = await alert_service.create_alert(
        session, setup["user"],
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "79000"},
        cooldown_seconds=1,
    )
    assert alert.cooldown_seconds >= 60


# --- Правка алерта ---


async def test_update_changes_condition(session, setup):
    alert = await alert_service.create_alert(
        session, setup["user"],
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "79000"},
    )
    await session.commit()

    await alert_service.update_alert(
        session, alert,
        market_id=setup["market"].id,
        type_code="price_below",
        params={"level": "70000"},
        cooldown_seconds=1800,
    )
    await session.commit()

    reloaded = await alert_service.get_alert(session, setup["user"], alert.id)
    assert reloaded.params == {"level": "70000"}
    assert reloaded.cooldown_seconds == 1800

    type_row = await session.get(AlertType, reloaded.alert_type_id)
    assert type_row.code == "price_below"


async def test_update_clears_cooldown_when_condition_changes(session, setup):
    """Новое условие не должно молчать из-за паузы, назначенной старому."""
    await alert_service.create_alert(
        session, setup["user"],
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "79000"},
    )
    await session.commit()

    fired = await alert_service.evaluate_all(session)
    await session.commit()
    assert len(fired) == 1

    alert = (await alert_service.list_alerts(session, setup["user"]))[0][0]
    assert alert.last_triggered_at is not None

    await alert_service.update_alert(
        session, alert,
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "75000"},
    )
    await session.commit()

    assert alert.last_triggered_at is None
    assert alert.trigger_count == 1, "история срабатываний не переписывается"

    assert len(await alert_service.evaluate_all(session)) == 1


async def test_update_keeps_cooldown_when_only_channels_change(session, setup):
    """Смена каналов доставки — не смена условия, пауза остаётся."""
    await alert_service.create_alert(
        session, setup["user"],
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "79000"},
    )
    await session.commit()

    await alert_service.evaluate_all(session)
    await session.commit()

    alert = (await alert_service.list_alerts(session, setup["user"]))[0][0]
    await alert_service.update_alert(
        session, alert,
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "79000"},
        notify_telegram=False,
    )
    await session.commit()

    assert alert.last_triggered_at is not None
    assert alert.notify_telegram is False
    assert await alert_service.evaluate_all(session) == []


async def test_update_rejects_bad_params(session, setup):
    alert = await alert_service.create_alert(
        session, setup["user"],
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "79000"},
    )
    await session.commit()

    with pytest.raises(alert_service.AlertError):
        await alert_service.update_alert(
            session, alert,
            market_id=setup["market"].id,
            type_code="price_above",
            params={"level": "-5"},
        )


async def test_recent_triggers_newest_first(session, setup):
    await alert_service.create_alert(
        session, setup["user"],
        market_id=setup["market"].id,
        type_code="price_above",
        params={"level": "79000"},
        cooldown_seconds=60,
    )
    await session.commit()
    await alert_service.evaluate_all(session)
    await session.commit()

    alert = (await alert_service.list_alerts(session, setup["user"]))[0][0]
    triggers = await alert_service.recent_triggers(session, alert)

    assert len(triggers) == 1
    assert "80000" in triggers[0].message
