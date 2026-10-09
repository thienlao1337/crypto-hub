"""Web push: browser subscriptions and sending notifications.

Push isn't a separate kind of event but a second way to deliver to the browser what
would have landed in the panel feed anyway: the same settings, the same show_web flag.
The difference is that the feed needs an open tab, while push arrives even when it's
closed.

VAPID keys: a pair the browser's push service uses to tell our server apart from anyone
else's. The public key goes to the browser on subscription, the private one signs every
send. No pair - web push is disabled, the other channels work as before.

pywebpush is synchronous inside (requests), so sending runs in a separate thread: a
blocking call in the shared loop would stall both exchange syncing and the alert engine.
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

# How long to wait for the push service to respond. Longer makes no sense: the
# notification isn't delivered instantly anyway, and a hung request ties up a
# thread.
SEND_TIMEOUT = 10

# How long the push service holds a notification for a sleeping device.
# pywebpush defaults to 0 - "deliver now or drop", so a phone locked at the
# moment of sending would never learn about a triggered alert. Five minutes: a
# price that hit its level half an hour ago is no longer news, but a minute's
# delay hurts nothing.
TTL_SECONDS = 300

# Codes after which the subscription should be deleted rather than retried: the
# device unsubscribed or the subscription expired.
GONE_STATUSES = (404, 410)


class WebPushError(Exception):
    """The subscription wasn't accepted or can't be saved."""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    # The subscription is dead - remove it instead of trying again.
    gone: bool = False
    error: str | None = None


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def public_key() -> str:
    return get_settings().vapid_public_key


# --- Subscriptions ---


async def subscribe(
    session: AsyncSession,
    user: User,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    label: str | None = None,
) -> PushSubscription:
    """Save a browser subscription.

    Resubscribing from the same device updates the row: the browser issues the same
    endpoint, and duplicates for one address must not pile up - the user would get every
    notification twice.
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


# --- Sending ---


async def send(
    *, endpoint: str, p256dh: str, auth: str, payload: dict
) -> SendResult:
    """Send one notification to one device."""
    if not is_configured():
        return SendResult(ok=False, error="Ключи VAPID не заданы.")

    return await asyncio.to_thread(_send_blocking, endpoint, p256dh, auth, payload)


def _send_blocking(endpoint: str, p256dh: str, auth: str, payload: dict) -> SendResult:
    # Imported inside the function: the library pulls in requests and
    # cryptography, and it's only needed when web push is actually configured.
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
        # The exception text may contain the push service's full response; we
        # store a short version in the database and the details in the log.
        logger.warning("Web push not delivered (%s): %s", status, exc)
        return SendResult(ok=False, error=f"Push-сервис ответил {status or 'ошибкой'}.")
    except Exception as exc:
        logger.warning("Web push not sent: %s", exc)
        return SendResult(ok=False, error=str(exc)[:200])


def mark_used(subscription: PushSubscription) -> None:
    subscription.last_used_at = datetime.now(timezone.utc)
    subscription.last_error = None


def generate_keys() -> tuple[str, str]:
    """A new VAPID key pair in the form the settings expect.

    The public key is an uncompressed P-256 curve point in base64url: exactly the form
    browser.pushManager.subscribe accepts. The private key is base64url too, as
    pywebpush reads it.
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
