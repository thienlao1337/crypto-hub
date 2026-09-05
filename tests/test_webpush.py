import base64

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import get_settings
from app.models import Notification, PushSubscription
from app.services import notification_service as ns
from app.services import user_service, webpush
from app.worker import delivery

ENDPOINT = "https://push.example.com/subscription/abc"


@pytest.fixture
def vapid(monkeypatch):
    """Ключи VAPID на время теста — настоящие, но одноразовые."""
    public, private = webpush.generate_keys()
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", public, raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", private, raising=False)
    monkeypatch.setattr(settings, "vapid_subject", "mailto:test@example.com", raising=False)
    return public, private


@pytest_asyncio.fixture
async def user(session):
    person = await user_service.create_user(
        session, email="pusher@example.com", password="pusher-password-1"
    )
    await session.commit()
    return person


# --- Ключи ---


def test_generated_public_key_is_uncompressed_point():
    """browser.pushManager.subscribe принимает только такой вид ключа."""
    public, private = webpush.generate_keys()

    raw = base64.urlsafe_b64decode(public + "=" * (-len(public) % 4))
    assert len(raw) == 65
    assert raw[0] == 0x04

    assert len(base64.urlsafe_b64decode(private + "=" * (-len(private) % 4))) == 32
    assert "=" not in public and "=" not in private


def test_disabled_without_keys(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", "", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "", raising=False)

    assert webpush.is_configured() is False


def test_enabled_with_keys(vapid):
    assert webpush.is_configured() is True


# --- Подписки ---


async def test_subscribe_stores_keys(session, user):
    subscription = await webpush.subscribe(
        session, user, endpoint=ENDPOINT, p256dh="key", auth="secret", label="Firefox"
    )
    await session.commit()

    assert subscription.user_id == user.id
    assert subscription.label == "Firefox"
    assert len(await webpush.list_subscriptions(session, user)) == 1


async def test_resubscribe_updates_instead_of_duplicating(session, user):
    """Иначе одно уведомление приходило бы на устройство дважды."""
    await webpush.subscribe(session, user, endpoint=ENDPOINT, p256dh="old", auth="a")
    await webpush.subscribe(session, user, endpoint=ENDPOINT, p256dh="new", auth="b")
    await session.commit()

    rows = (await session.execute(select(PushSubscription))).scalars().all()
    assert len(rows) == 1
    assert rows[0].p256dh == "new"


async def test_incomplete_subscription_rejected(session, user):
    with pytest.raises(webpush.WebPushError):
        await webpush.subscribe(session, user, endpoint="", p256dh="k", auth="a")


async def test_unsubscribe_removes_own_only(session, user):
    await webpush.subscribe(session, user, endpoint=ENDPOINT, p256dh="k", auth="a")
    stranger = await user_service.create_user(
        session, email="other@example.com", password="other-password-1"
    )
    await session.commit()

    assert await webpush.unsubscribe(session, stranger, ENDPOINT) is False
    assert await webpush.unsubscribe(session, user, ENDPOINT) is True
    assert await webpush.list_subscriptions(session, user) == []


# --- Отправка ---


async def test_send_is_disabled_without_keys(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", "", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "", raising=False)

    result = await webpush.send(endpoint=ENDPOINT, p256dh="k", auth="a", payload={})
    assert result.ok is False


def test_gone_status_marks_subscription_dead(vapid, monkeypatch):
    """404 и 410 означают «устройство отписалось», повторять нечего."""

    class FakeResponse:
        status_code = 410

    from pywebpush import WebPushException

    def explode(*args, **kwargs):
        raise WebPushException("gone", response=FakeResponse())

    monkeypatch.setattr("pywebpush.webpush", explode)

    result = webpush._send_blocking(ENDPOINT, "k", "a", {"title": "t"})
    assert result.gone is True


def test_other_errors_do_not_kill_subscription(vapid, monkeypatch):
    class FakeResponse:
        status_code = 500

    from pywebpush import WebPushException

    def explode(*args, **kwargs):
        raise WebPushException("boom", response=FakeResponse())

    monkeypatch.setattr("pywebpush.webpush", explode)

    result = webpush._send_blocking(ENDPOINT, "k", "a", {"title": "t"})
    assert result.ok is False
    assert result.gone is False


def test_error_text_does_not_leak_push_service_response(vapid, monkeypatch):
    """В базу кладём короткое, подробности остаются в логах."""

    class FakeResponse:
        status_code = 400

    from pywebpush import WebPushException

    def explode(*args, **kwargs):
        raise WebPushException("Bearer eyJhbGciOi... целиком ответ сервиса", response=FakeResponse())

    monkeypatch.setattr("pywebpush.webpush", explode)

    result = webpush._send_blocking(ENDPOINT, "k", "a", {"title": "t"})
    assert "eyJhbGciOi" not in (result.error or "")


# --- Доставка из очереди ---


async def test_delivery_skipped_when_push_not_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", "", raising=False)
    monkeypatch.setattr(settings, "vapid_private_key", "", raising=False)

    assert await delivery.deliver_web_push() == 0


async def test_hidden_notification_is_not_queued_for_push(session, user):
    """Что не показывается в ленте, то и не пушится."""
    await ns.push(
        session, user_id=user.id, kind=ns.KIND_ALERT, title="t", body="b", show_web=False
    )
    await session.commit()

    row = (await session.execute(select(Notification))).scalar_one()
    assert row.delivered_push is True


async def test_visible_notification_waits_for_push(session, user):
    await ns.push(session, user_id=user.id, kind=ns.KIND_ALERT, title="t", body="b")
    await session.commit()

    row = (await session.execute(select(Notification))).scalar_one()
    assert row.delivered_push is False


async def test_delivery_sends_to_every_device_and_marks_done(session, user, vapid, monkeypatch):
    await webpush.subscribe(session, user, endpoint=ENDPOINT + "/1", p256dh="k", auth="a")
    await webpush.subscribe(session, user, endpoint=ENDPOINT + "/2", p256dh="k", auth="a")
    await ns.push(session, user_id=user.id, kind=ns.KIND_ALERT, title="t", body="b")
    await session.commit()

    calls = []

    async def fake_send(**kwargs):
        calls.append(kwargs["endpoint"])
        return webpush.SendResult(ok=True)

    monkeypatch.setattr(webpush, "send", fake_send)
    _use_test_session(monkeypatch, session)

    sent = await delivery.deliver_web_push()

    assert sent == 2
    assert sorted(calls) == [ENDPOINT + "/1", ENDPOINT + "/2"]

    row = (await session.execute(select(Notification))).scalar_one()
    assert row.delivered_push is True


async def test_delivery_drops_dead_subscription(session, user, vapid, monkeypatch):
    """Мёртвые подписки копились бы при каждой смене браузера."""
    await webpush.subscribe(session, user, endpoint=ENDPOINT, p256dh="k", auth="a")
    await ns.push(session, user_id=user.id, kind=ns.KIND_ALERT, title="t", body="b")
    await session.commit()

    async def fake_send(**kwargs):
        return webpush.SendResult(ok=False, gone=True, error="Подписка больше не действует.")

    monkeypatch.setattr(webpush, "send", fake_send)
    _use_test_session(monkeypatch, session)

    assert await delivery.deliver_web_push() == 0
    assert (await session.execute(select(PushSubscription))).scalars().all() == []


async def test_delivery_keeps_subscription_on_temporary_error(session, user, vapid, monkeypatch):
    await webpush.subscribe(session, user, endpoint=ENDPOINT, p256dh="k", auth="a")
    await ns.push(session, user_id=user.id, kind=ns.KIND_ALERT, title="t", body="b")
    await session.commit()

    async def fake_send(**kwargs):
        return webpush.SendResult(ok=False, error="Push-сервис ответил 500.")

    monkeypatch.setattr(webpush, "send", fake_send)
    _use_test_session(monkeypatch, session)

    await delivery.deliver_web_push()

    subscription = (await session.execute(select(PushSubscription))).scalar_one()
    assert subscription.last_error == "Push-сервис ответил 500."
    # Повторять не станем: пуш ценен свежестью.
    row = (await session.execute(select(Notification))).scalar_one()
    assert row.delivered_push is True


def _use_test_session(monkeypatch, session):
    """Подсунуть задаче доставки сессию теста.

    Фоновая задача открывает свои сессии через session_scope, а тестовая
    база живёт во внешней транзакции — без подмены задача не увидела бы
    ничего из того, что тест только что записал.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(delivery, "session_scope", scope)


# --- Настоящий запрос к push-сервису ---


def make_receiver_keys() -> tuple[str, str]:
    """Ключи, какие выдал бы браузер: точка P-256 и случайный секрет."""
    import os

    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    raw = b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")

    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    return encode(raw), encode(os.urandom(16))


def test_request_to_push_service_is_signed_and_encrypted(vapid):
    """Проверка сборки запроса без браузера.

    Поднимаем свой «push-сервис» и смотрим, что уходит: подпись VAPID,
    шифрование по RFC 8291 и отсутствие открытого текста в теле. Это
    единственное, что можно проверить без настоящей подписки браузера,
    и именно здесь ломается интеграция, если формат ключей неверен.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            captured["headers"] = dict(self.headers)
            captured["body"] = self.rfile.read(length)
            self.send_response(201)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    endpoint = f"http://127.0.0.1:{server.server_address[1]}/push/xyz"
    p256dh, auth = make_receiver_keys()

    try:
        result = webpush._send_blocking(
            endpoint, p256dh, auth, {"title": "Сработал алерт", "body": "BTC выше 70000"}
        )
    finally:
        thread.join(timeout=5)
        server.server_close()

    assert result.ok is True, result.error

    headers = {key.lower(): value for key, value in captured["headers"].items()}
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["authorization"].startswith("vapid ")
    # k= несёт наш публичный ключ: по нему push-сервис проверяет подпись.
    assert "t=" in headers["authorization"] and "k=" in headers["authorization"]
    # TTL по умолчанию нулевой: push-сервис выбросил бы уведомление,
    # если устройство в этот момент спит.
    assert int(headers["ttl"]) == webpush.TTL_SECONDS

    body = captured["body"]
    assert body, "тело не должно быть пустым"
    assert "Сработал алерт".encode() not in body, "полезная нагрузка обязана быть зашифрована"
