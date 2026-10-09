"""Tests through the real ASGI stack.

They catch what service tests don't see: router behaviour, the session, CSRF and
template rendering. This is exactly where it surfaced that loaded objects must not be
accessed after session.rollback() - the page failed with MissingGreenlet and the user
saw "Something went wrong".
"""

from decimal import Decimal

import httpx
import pyotp
import pytest_asyncio
from sqlalchemy import select

from app.db import get_session
from app.exchanges.base import KeyCheck
from app.models import AlertType, Exchange, MarketTicker, User
from app.services import alert_service, invite_service, market_service
from app.services import exchange_keys_service as keys_service
from app.services import notification_service, user_service
from app.web.main import app
from tests import fakes

PASSWORD = "owner-password-1"


@pytest_asyncio.fixture
async def client(session):
    """A client for the app running on the test session.

    https scheme so the session cookie with the Secure flag is kept regardless of the
    environment settings.
    """

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test", follow_redirects=False
    ) as http_client:
        yield http_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def logged_in(client, session):
    session.add(Exchange(code="bybit", name="Bybit", sort_order=10))
    session.add(Exchange(code="binance", name="Binance", sort_order=20))
    await user_service.create_user(session, email="owner@example.com", password=PASSWORD)
    await session.commit()

    page = await client.get("/login")
    response = await client.post(
        "/login",
        data={
            "email": "owner@example.com",
            "password": PASSWORD,
            "csrf_token": _csrf(page.text),
        },
    )
    assert response.status_code == 303, response.text[:400]
    return client


def _csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def _accepting(can_trade: bool = False):
    async def check(*args, **kwargs):
        return KeyCheck(is_valid=True, can_trade=can_trade, permissions_known=True)

    return check


async def _rejecting(*args, **kwargs):
    return KeyCheck(is_valid=False, error="Биржа отклонила ключ")


# --- Login ---


async def test_login_and_dashboard(logged_in):
    page = await logged_in.get("/")
    assert page.status_code == 200
    assert "Дашборд" in page.text


async def test_anonymous_is_sent_to_login(client):
    response = await client.get("/portfolio")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_form_without_csrf_is_rejected(logged_in):
    response = await logged_in.post(
        "/settings/keys",
        data={
            "exchange_code": "bybit",
            "api_key": "k",
            "api_secret": "s",
            "csrf_token": "подделка",
        },
    )
    assert response.status_code == 400
    assert "устарела" in response.text


# --- Exchange keys ---


async def test_invalid_key_redirects_with_message(logged_in, monkeypatch):
    """An exchange rejection must not crash the page.

    There used to be a 500 here: the handler did a rollback and immediately re-read
    data, and a rollback marks loaded objects as expired.
    """
    monkeypatch.setattr(keys_service, "_check_with_exchange", _rejecting)

    page = await logged_in.get("/settings/keys")
    response = await logged_in.post(
        "/settings/keys",
        data={
            "exchange_code": "bybit",
            "api_key": "bad-key",
            "api_secret": "bad-secret",
            "label": "проба",
            "csrf_token": _csrf(page.text),
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings/keys"

    # The message survives the redirect and is shown exactly once.
    page = await logged_in.get("/settings/keys")
    assert "Биржа отклонила ключ" in page.text
    again = await logged_in.get("/settings/keys")
    assert "Биржа отклонила ключ" not in again.text


async def test_empty_key_is_rejected_without_network(logged_in):
    page = await logged_in.get("/settings/keys")
    response = await logged_in.post(
        "/settings/keys",
        data={
            "exchange_code": "bybit",
            "api_key": "   ",
            "api_secret": "   ",
            "label": "пусто",
            "csrf_token": _csrf(page.text),
        },
    )
    assert response.status_code == 303

    page = await logged_in.get("/settings/keys")
    assert "Заполните и ключ, и секрет." in page.text


async def test_key_added_successfully(logged_in, monkeypatch):
    monkeypatch.setattr(keys_service, "_check_with_exchange", _accepting())

    page = await logged_in.get("/settings/keys")
    response = await logged_in.post(
        "/settings/keys",
        data={
            "exchange_code": "bybit",
            "api_key": "good-key-1234",
            "api_secret": "good-secret",
            "label": "основной",
            "csrf_token": _csrf(page.text),
        },
    )
    assert response.status_code == 303

    page = await logged_in.get("/settings/keys")
    assert "Ключ подключён." in page.text
    assert "…1234" in page.text
    assert "good-key-1234" not in page.text, "полный ключ не должен попадать в разметку"


async def test_requested_trading_without_confirmation_warns(logged_in, monkeypatch):
    monkeypatch.setattr(keys_service, "_check_with_exchange", _accepting(can_trade=False))

    page = await logged_in.get("/settings/keys")
    await logged_in.post(
        "/settings/keys",
        data={
            "exchange_code": "bybit",
            "api_key": "good-key-5678",
            "api_secret": "good-secret",
            "label": "торговый",
            "want_trading": "true",
            "csrf_token": _csrf(page.text),
        },
    )

    page = await logged_in.get("/settings/keys")
    assert "не подтвердила право на" in page.text
    assert "только чтение" in page.text


# --- Password change ---


async def test_wrong_current_password_redirects_with_message(logged_in):
    page = await logged_in.get("/settings/security")
    response = await logged_in.post(
        "/settings/password",
        data={
            "current_password": "не-тот-пароль",
            "new_password": "новый-длинный-пароль",
            "new_password_repeat": "новый-длинный-пароль",
            "csrf_token": _csrf(page.text),
        },
    )
    assert response.status_code == 303

    page = await logged_in.get("/settings/security")
    assert "Текущий пароль указан неверно." in page.text


async def test_password_changed(logged_in):
    page = await logged_in.get("/settings/security")
    response = await logged_in.post(
        "/settings/password",
        data={
            "current_password": PASSWORD,
            "new_password": "новый-длинный-пароль",
            "new_password_repeat": "новый-длинный-пароль",
            "csrf_token": _csrf(page.text),
        },
    )
    assert response.status_code == 303

    page = await logged_in.get("/settings/security")
    assert "Пароль изменён." in page.text


# --- Portfolio ---


async def test_portfolio_without_accounts(logged_in):
    page = await logged_in.get("/portfolio")
    assert page.status_code == 200
    assert "Нет подключений" in page.text


# --- Alerts ---


@pytest_asyncio.fixture
async def alert(logged_in, session):
    """One alert on BTC/USDT, so there's something to edit."""
    for order, (code, name) in enumerate(
        [
            ("price_above", "Цена выше уровня"),
            ("price_below", "Цена ниже уровня"),
            ("pct_change", "Изменение в процентах"),
            ("rsi", "Уровень RSI"),
        ]
    ):
        session.add(AlertType(code=code, name=name, sort_order=order))
    await session.flush()

    exchange = await session.scalar(select(Exchange).where(Exchange.code == "bybit"))
    await market_service.sync_markets(
        session, exchange, fakes.FakeAdapter(markets=[fakes.market("BTC/USDT", "BTC", "USDT")])
    )
    market = await market_service.get_market(session, exchange.id, "BTC/USDT")
    session.add(MarketTicker(market_id=market.id, last=Decimal("80000")))

    user = await session.scalar(select(User).where(User.email == "owner@example.com"))
    row = await alert_service.create_alert(
        session, user,
        market_id=market.id,
        type_code="price_above",
        params={"level": "79000"},
    )
    await session.commit()

    # Return plain values, not ORM objects: on an input error the router does a
    # rollback, and a loaded object expires after that - the test would fail on
    # accessing its field instead of on checking behaviour.
    return {"alert_id": row.id, "market_id": market.id, "user_id": user.id}


async def test_alerts_page_lists_edit_link(logged_in, alert):
    page = await logged_in.get("/alerts")

    assert page.status_code == 200
    assert f'/alerts/{alert["alert_id"]}/edit' in page.text


async def test_edit_form_is_prefilled(logged_in, alert):
    page = await logged_in.get(f'/alerts/{alert["alert_id"]}/edit')

    assert page.status_code == 200
    assert 'value="79000"' in page.text


async def test_alert_edited_through_form(logged_in, session, alert):
    page = await logged_in.get(f'/alerts/{alert["alert_id"]}/edit')
    response = await logged_in.post(
        f'/alerts/{alert["alert_id"]}',
        data={
            "market_id": alert["market_id"],
            "type_code": "price_below",
            "level": "70000",
            "cooldown_minutes": 30,
            "notify_web": "true",
            "csrf_token": _csrf(page.text),
        },
    )

    assert response.status_code == 303
    user = await session.get(User, alert["user_id"])
    reloaded = await alert_service.get_alert(session, user, alert["alert_id"])
    assert reloaded.params == {"level": "70000"}
    assert reloaded.cooldown_seconds == 1800
    # The Telegram checkbox was unchecked - the form must convey that, not ignore it.
    assert reloaded.notify_telegram is False


async def test_bad_edit_returns_message_not_500(logged_in, alert):
    """After a rollback the service must not crash the page by accessing the alert."""
    page = await logged_in.get(f'/alerts/{alert["alert_id"]}/edit')
    response = await logged_in.post(
        f'/alerts/{alert["alert_id"]}',
        data={
            "market_id": alert["market_id"],
            "type_code": "price_above",
            "level": "-1",
            "cooldown_minutes": 30,
            "csrf_token": _csrf(page.text),
        },
    )

    assert response.status_code == 303
    follow = await logged_in.get(f'/alerts/{alert["alert_id"]}/edit')
    assert "Укажите положительный уровень цены." in follow.text


async def test_stranger_cannot_edit_alert(logged_in, session, alert):
    """Someone else's alert must not open for editing via a direct link."""
    await user_service.create_user(
        session, email="stranger@example.com", password="stranger-password-1"
    )
    await session.commit()

    page = await logged_in.get("/alerts")
    await logged_in.post("/logout", data={"csrf_token": _csrf(page.text)})

    login = await logged_in.get("/login")
    entered = await logged_in.post(
        "/login",
        data={
            "email": "stranger@example.com",
            "password": "stranger-password-1",
            "csrf_token": _csrf(login.text),
        },
    )
    assert entered.status_code == 303

    response = await logged_in.get(f'/alerts/{alert["alert_id"]}/edit')
    assert response.status_code == 303
    assert response.headers["location"] == "/alerts"


# --- Notification settings ---


async def test_notification_settings_page(logged_in):
    page = await logged_in.get("/settings/notifications")

    assert page.status_code == 200
    assert "Сработавшие алерты" in page.text
    # Nothing configured - so everything is enabled.
    assert page.text.count("checked") == len(notification_service.EVENT_KINDS) * len(
        notification_service.CHANNELS
    )


async def test_notification_settings_saved(logged_in, session):
    page = await logged_in.get("/settings/notifications")
    response = await logged_in.post(
        "/settings/notifications",
        data={
            "alert:web": "true",
            "signal:telegram": "true",
            "csrf_token": _csrf(page.text),
        },
    )

    assert response.status_code == 303

    user = await session.scalar(select(User).where(User.email == "owner@example.com"))
    matrix = await notification_service.settings_matrix(session, user)
    assert matrix[("alert", "web")] is True
    assert matrix[("alert", "telegram")] is False
    assert matrix[("signal", "telegram")] is True
    assert matrix[("signal", "web")] is False


async def test_portfolio_sync_rejects_forged_request(logged_in):
    """The "Refresh" button hits the exchanges - it must not work via a link from another site."""
    response = await logged_in.post("/portfolio/sync", data={"csrf_token": "чужой"})

    assert response.status_code == 400


# --- Invite-based registration ---


async def test_registration_by_invite_logs_the_user_in(logged_in, session):
    """A new user's whole path: code -> form -> logged in."""
    owner = await session.scalar(select(User).where(User.email == "owner@example.com"))
    invite = await invite_service.create_invite(session, created_by=owner)
    code = invite.code
    await session.commit()

    # We go to a panel page for the token: /login redirects a logged-in user,
    # so there'd be no markup to take it from.
    page = await logged_in.get("/")
    await logged_in.post("/logout", data={"csrf_token": _csrf(page.text)})

    form = await logged_in.get(f"/register?code={code}")
    assert form.status_code == 200

    response = await logged_in.post(
        "/register",
        data={
            "code": code,
            "email": "newcomer@example.com",
            "password": "newcomer-password-1",
            "password_repeat": "newcomer-password-1",
            "csrf_token": _csrf(form.text),
        },
    )

    assert response.status_code == 303, response.text[:400]
    assert response.headers["location"] == "/"

    dashboard = await logged_in.get("/")
    assert dashboard.status_code == 200


async def test_registration_without_invite_is_refused(client, session):
    """Registration is closed: without a code there's no way in."""
    session.add(Exchange(code="bybit", name="Bybit", sort_order=10))
    await session.commit()

    form = await client.get("/register")
    response = await client.post(
        "/register",
        data={
            "code": "выдуманный-код",
            "email": "stranger@example.com",
            "password": "stranger-password-1",
            "password_repeat": "stranger-password-1",
            "csrf_token": _csrf(form.text),
        },
    )

    assert response.status_code == 400
    assert await session.scalar(
        select(User).where(User.email == "stranger@example.com")
    ) is None


async def test_invite_cannot_be_used_twice(logged_in, session):
    """Otherwise a single leaked link opens the panel to anyone."""
    owner = await session.scalar(select(User).where(User.email == "owner@example.com"))
    invite = await invite_service.create_invite(session, created_by=owner)
    code = invite.code
    await session.commit()

    page = await logged_in.get("/")
    await logged_in.post("/logout", data={"csrf_token": _csrf(page.text)})

    async def register(email: str):
        form = await logged_in.get("/register")
        return await logged_in.post(
            "/register",
            data={
                "code": code,
                "email": email,
                "password": "some-password-12",
                "password_repeat": "some-password-12",
                "csrf_token": _csrf(form.text),
            },
        )

    assert (await register("first@example.com")).status_code == 303

    page = await logged_in.get("/")
    await logged_in.post("/logout", data={"csrf_token": _csrf(page.text)})

    assert (await register("second@example.com")).status_code == 400


# --- Login with a second factor ---


@pytest_asyncio.fixture
async def with_totp(logged_in, session):
    """An owner with 2FA enabled, and their secret."""
    user = await session.scalar(select(User).where(User.email == "owner@example.com"))
    secret, _ = user_service.begin_totp_setup(user)
    await user_service.confirm_totp(
        session, user, secret=secret, code=pyotp.TOTP(secret).now()
    )
    await session.commit()

    page = await logged_in.get("/")
    await logged_in.post("/logout", data={"csrf_token": _csrf(page.text)})
    return secret


async def test_password_alone_does_not_open_the_panel(logged_in, with_totp):
    page = await logged_in.get("/login")
    response = await logged_in.post(
        "/login",
        data={
            "email": "owner@example.com",
            "password": PASSWORD,
            "csrf_token": _csrf(page.text),
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login/2fa"

    # Until the code is entered, access must not be granted.
    dashboard = await logged_in.get("/")
    assert dashboard.status_code == 303


async def test_correct_code_completes_login(logged_in, with_totp):
    page = await logged_in.get("/login")
    await logged_in.post(
        "/login",
        data={
            "email": "owner@example.com",
            "password": PASSWORD,
            "csrf_token": _csrf(page.text),
        },
    )

    form = await logged_in.get("/login/2fa")
    response = await logged_in.post(
        "/login/2fa",
        data={"code": pyotp.TOTP(with_totp).now(), "csrf_token": _csrf(form.text)},
    )

    assert response.status_code == 303
    assert (await logged_in.get("/")).status_code == 200


async def test_wrong_code_keeps_the_door_shut(logged_in, with_totp):
    page = await logged_in.get("/login")
    await logged_in.post(
        "/login",
        data={
            "email": "owner@example.com",
            "password": PASSWORD,
            "csrf_token": _csrf(page.text),
        },
    )

    form = await logged_in.get("/login/2fa")
    response = await logged_in.post(
        "/login/2fa",
        data={"code": "000000", "csrf_token": _csrf(form.text)},
    )

    assert response.status_code == 401
    assert (await logged_in.get("/")).status_code == 303


async def test_second_factor_page_needs_a_started_login(client, session):
    """The 2FA step can't be opened directly without entering the password."""
    session.add(Exchange(code="bybit", name="Bybit", sort_order=10))
    await user_service.create_user(session, email="owner@example.com", password=PASSWORD)
    await session.commit()

    response = await client.get("/login/2fa")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
