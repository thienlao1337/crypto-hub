"""Веб-пуш: подписки браузеров и отправка уведомлений.

Пуш — не отдельный вид события, а второй способ доставить в браузер то,
что и так попало бы в ленту панели: те же настройки, тот же признак
show_web. Разница в том, что лента требует открытой вкладки, а пуш
доходит и при закрытой.

Ключи VAPID: пара, которой push-сервис браузера отличает наш сервер от
чужого. Публичный уходит в браузер при подписке, приватным подписывается
каждая отправка. Пары нет — веб-пуш выключен, остальные каналы работают
как работали.

pywebpush внутри синхронный (requests), поэтому отправка уходит в
отдельный поток: блокирующий вызов в общем цикле остановил бы и
синхронизацию бирж, и движок алертов.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import PushSubscription, User

logger = logging.getLogger(__name__)

# Сколько ждать ответа push-сервиса. Больше нет смысла: уведомление и
# так доставляется не мгновенно, а зависший запрос держит поток.
SEND_TIMEOUT = 10

# Сколько push-сервис держит уведомление для спящего устройства. По
# умолчанию pywebpush ставит 0 — «доставить сейчас или выбросить», и
# телефон, заблокированный в момент отправки, не узнал бы о сработавшем
# алерте вовсе. Пять минут: цена, дошедшая до уровня полчаса назад, —
# уже не новость, а вот минутная пауза ничего не портит.
TTL_SECONDS = 300

# Коды, после которых подписку надо удалить, а не повторять отправку:
# устройство отписалось или подписка протухла.
GONE_STATUSES = (404, 410)


class WebPushError(Exception):
    """Подписка не принята или не может быть сохранена."""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    # Подписка мертва — её надо убрать, а не пытаться снова.
    gone: bool = False
    error: str | None = None


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def public_key() -> str:
    return get_settings().vapid_public_key


# --- Подписки ---


async def subscribe(
    session: AsyncSession,
    user: User,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    label: str | None = None,
) -> PushSubscription:
    """Сохранить подписку браузера.

    Повторная подписка с того же устройства обновляет строку: браузер
    выдаёт тот же endpoint, и плодить дубли по одному адресу нельзя —
    пользователь получал бы каждое уведомление дважды.
    """
    endpoint = (endpoint or "").strip()
    if not endpoint or not p256dh or not auth:
        raise WebPushError("Браузер прислал неполную подписку.")

    existing = await session.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )
    if existing is None:
        existing = PushSubscription(endpoint=endpoint)
        session.add(existing)

    existing.user_id = user.id
    existing.p256dh = p256dh
    existing.auth = auth
    existing.label = (label or "")[:255] or None
    existing.last_error = None

    await session.flush()
    return existing


async def unsubscribe(session: AsyncSession, user: User, endpoint: str) -> bool:
    subscription = await session.scalar(
        select(PushSubscription).where(
            PushSubscription.endpoint == endpoint,
            PushSubscription.user_id == user.id,
        )
    )
    if subscription is None:
        return False

    await session.delete(subscription)
    await session.flush()
    return True


async def list_subscriptions(session: AsyncSession, user: User) -> list[PushSubscription]:
    result = await session.execute(
        select(PushSubscription)
        .where(PushSubscription.user_id == user.id)
        .order_by(PushSubscription.created_at.desc())
    )
    return list(result.scalars())


# --- Отправка ---


async def send(
    *, endpoint: str, p256dh: str, auth: str, payload: dict
) -> SendResult:
    """Отправить одно уведомление на одно устройство."""
    if not is_configured():
        return SendResult(ok=False, error="Ключи VAPID не заданы.")

    return await asyncio.to_thread(_send_blocking, endpoint, p256dh, auth, payload)


def _send_blocking(endpoint: str, p256dh: str, auth: str, payload: dict) -> SendResult:
    # Импорт внутри функции: библиотека тянет requests и криптографию,
    # а нужна только когда веб-пуш действительно настроен.
    from pywebpush import WebPushException, webpush

    settings = get_settings()
    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},
            ttl=TTL_SECONDS,
            timeout=SEND_TIMEOUT,
        )
        return SendResult(ok=True)
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in GONE_STATUSES:
            return SendResult(ok=False, gone=True, error="Подписка больше не действует.")
        # В тексте исключения бывает полный ответ push-сервиса; в базу
        # кладём короткое, подробности — в лог.
        logger.warning("Веб-пуш не доставлен (%s): %s", status, exc)
        return SendResult(ok=False, error=f"Push-сервис ответил {status or 'ошибкой'}.")
    except Exception as exc:
        logger.warning("Веб-пуш не отправлен: %s", exc)
        return SendResult(ok=False, error=str(exc)[:200])


def mark_used(subscription: PushSubscription) -> None:
    subscription.last_used_at = datetime.now(timezone.utc)
    subscription.last_error = None


def generate_keys() -> tuple[str, str]:
    """Новая пара ключей VAPID в том виде, в каком её ждут настройки.

    Публичный — несжатая точка кривой P-256 в base64url: именно такой
    вид принимает browser.pushManager.subscribe. Приватный — тоже
    base64url, как его читает pywebpush.
    """
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    public_numbers = private.public_key().public_numbers()

    raw_public = (
        b"\x04"
        + public_numbers.x.to_bytes(32, "big")
        + public_numbers.y.to_bytes(32, "big")
    )
    raw_private = private.private_numbers().private_value.to_bytes(32, "big")

    def encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return encode(raw_public), encode(raw_private)


if __name__ == "__main__":
    public, private = generate_keys()
    print("Добавьте в .env:\n")
    print(f"VAPID_PUBLIC_KEY={public}")
    print(f"VAPID_PRIVATE_KEY={private}")
    print("VAPID_SUBJECT=mailto:ваша-почта@example.com")
