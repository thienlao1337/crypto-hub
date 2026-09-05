"""Проверки через настоящий ASGI-стек.

Ловят то, чего не видят тесты сервисов: поведение роутеров, сессию,
CSRF и отрисовку шаблонов. Именно здесь всплыло, что после
session.rollback() нельзя обращаться к загруженным объектам — страница
падала с MissingGreenlet, а пользователь видел «Что-то сломалось».
"""

import httpx
import pytest_asyncio

from app.db import get_session
from app.exchanges.base import KeyCheck
from app.models import Exchange
from app.services import exchange_keys_service as keys_service
from app.services import user_service
from app.web.main import app

PASSWORD = "owner-password-1"


@pytest_asyncio.fixture
async def client(session):
    """Клиент к приложению, работающий на тестовой сессии.

    Схема https, чтобы сессионная cookie с флагом Secure сохранялась
    независимо от настроек окружения.
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


# --- Вход ---


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


# --- Ключи бирж ---


async def test_invalid_key_redirects_with_message(logged_in, monkeypatch):
    """Отказ биржи не должен ронять страницу.

    Раньше здесь была пятисотка: обработчик делал rollback и тут же
    перечитывал данные, а откат помечает загруженные объекты протухшими.
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

    # Сообщение переживает редирект и показывается ровно один раз.
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


# --- Смена пароля ---


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


# --- Портфель ---


async def test_portfolio_without_accounts(logged_in):
    page = await logged_in.get("/portfolio")
    assert page.status_code == 200
    assert "Нет подключений" in page.text
