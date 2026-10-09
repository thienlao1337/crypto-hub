"""Notification delivery: Telegram and web push.

Separate from recording the event: a triggered alert lands in the feed immediately and
goes to the chat on the next pass. If the bot is unavailable or the user blocked it, the
event isn't lost and doesn't block the alert engine.

Who is allowed what isn't decided here: a notification for a disabled channel is marked
delivered at recording time and never enters the queue.
"""

import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import AlertTrigger, Notification, PushSubscription, User
from app.services import webpush

logger = logging.getLogger(__name__)
settings = get_settings()

BATCH_SIZE = 30
# Telegram rate-limits messages; a pause between sends keeps us safely below
# the limit without a separate queue.
SEND_DELAY = 0.05


async def deliver_pending() -> int:
    """Send notifications that haven't gone to Telegram yet."""
    if not settings.bot_token:
        return 0

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Notification.id, Notification.title, Notification.body,
                       Notification.payload, User.telegram_id)
                .join(User, User.id == Notification.user_id)
                .where(
                    Notification.delivered_telegram.is_(False),
                    User.telegram_id.is_not(None),
                    User.is_active.is_(True),
                )
                .order_by(Notification.id)
                .limit(BATCH_SIZE)
            )
        ).all()

    if not rows:
        return 0

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    sent = 0

    try:
        for notification_id, title, body, payload, chat_id in rows:
            try:
                await bot.send_message(chat_id, f"<b>{title}</b>\n{body}")
                await _mark(notification_id, delivered=True, error=None)
                await _mark_trigger(payload)
                sent += 1
            except TelegramForbiddenError:
                # The user blocked the bot. Retrying is pointless: mark it
                # delivered with an explanation, otherwise the queue would grow
                # forever.
                await _mark(
                    notification_id,
                    delivered=True,
                    error="Пользователь заблокировал бота.",
                )
            except TelegramRetryAfter as exc:
                logger.info("Telegram asks to wait %s s", exc.retry_after)
                break
            except Exception as exc:
                logger.warning("Notification %s not delivered: %s", notification_id, exc)
                await _mark(notification_id, delivered=False, error=str(exc))
    finally:
        await bot.session.close()

    if sent:
        logger.info("Sent to Telegram: %s", sent)
    return sent


async def _mark_trigger(payload: dict | None) -> None:
    """Mark delivery on the alert trigger, if it's known.

    The notification and the trigger are different rows: the first lives in the send
    queue, the second in the alert history. Without this mark the history would say
    nothing about whether the message went out.
    """
    trigger_id = (payload or {}).get("trigger_id")
    if not trigger_id:
        return

    async with session_scope() as session:
        trigger = await session.get(AlertTrigger, trigger_id)
        if trigger is None:
            return
        trigger.delivered_telegram = True
        await session.commit()


async def _mark(notification_id: int, *, delivered: bool, error: str | None) -> None:
    async with session_scope() as session:
        notification = await session.get(Notification, notification_id)
        if notification is None:
            return
        notification.delivered_telegram = delivered
        notification.delivery_error = error[:1000] if error else None
        await session.commit()


async def deliver_web_push() -> int:
    """Send notifications to subscribed browsers.

    Push follows the same events as the feed: showing something in the panel but not
    bringing it to the browser when the user asked for exactly that would be a
    half-measure. Devices the push service declared invalid are deleted right away:
    retrying them is pointless, and they'd pile up with every browser change.
    """
    if not webpush.is_configured():
        return 0

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Notification.id,
                    Notification.title,
                    Notification.body,
                    Notification.kind,
                    Notification.user_id,
                )
                .where(
                    Notification.delivered_push.is_(False),
                    Notification.show_web.is_(True),
                )
                .order_by(Notification.id)
                .limit(BATCH_SIZE)
            )
        ).all()

    if not rows:
        return 0

    sent = 0
    for notification_id, title, body, kind, user_id in rows:
        async with session_scope() as session:
            targets = (
                await session.execute(
                    select(PushSubscription).where(PushSubscription.user_id == user_id)
                )
            ).scalars().all()
            # Read the values up front: after a possible rollback the objects
            # expire, and accessing their fields would fail.
            devices = [(t.id, t.endpoint, t.p256dh, t.auth) for t in targets]

        payload = {
            "title": title,
            "body": body,
            # The tag merges repeats of one event into a single card.
            "tag": f"{kind}-{notification_id}",
            "url": "/notifications",
        }

        for device_id, endpoint, p256dh, auth_key in devices:
            result = await webpush.send(
                endpoint=endpoint, p256dh=p256dh, auth=auth_key, payload=payload
            )
            await _record_push(device_id, result)
            if result.ok:
                sent += 1

        await _mark_push(notification_id)

    if sent:
        logger.info("Sent via web push: %s", sent)
    return sent


async def _record_push(subscription_id: int, result: webpush.SendResult) -> None:
    async with session_scope() as session:
        subscription = await session.get(PushSubscription, subscription_id)
        if subscription is None:
            return

        if result.gone:
            await session.delete(subscription)
        elif result.ok:
            webpush.mark_used(subscription)
        else:
            subscription.last_error = (result.error or "")[:1000] or None

        await session.commit()


async def _mark_push(notification_id: int) -> None:
    """Mark that an attempt was made.

    We won't retry a failed send: push is valuable for being fresh, and a queue of
    yesterday's notifications isn't notifications anymore.
    """
    async with session_scope() as session:
        notification = await session.get(Notification, notification_id)
        if notification is None:
            return
        notification.delivered_push = True
        await session.commit()
