"""Доставка уведомлений: Telegram и веб-пуш.

Отдельно от записи события: сработавший алерт попадает в ленту сразу, а
в чат уходит следующим проходом. Если бот недоступен или пользователь
заблокировал его, событие не теряется и не блокирует движок алертов.

Кому что разрешено, здесь не решается: уведомление с выключенным
каналом помечается доставленным ещё при записи и в очередь не попадает.
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
# Telegram ограничивает частоту сообщений; пауза между отправками
# держит нас заведомо ниже лимита без отдельной очереди.
SEND_DELAY = 0.05


async def deliver_pending() -> int:
    """Разослать уведомления, которые ещё не ушли в Telegram."""
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
                # Пользователь заблокировал бота. Повторять бессмысленно:
                # помечаем доставленным с пояснением, иначе очередь будет
                # расти вечно.
                await _mark(
                    notification_id,
                    delivered=True,
                    error="Пользователь заблокировал бота.",
                )
            except TelegramRetryAfter as exc:
                logger.info("Telegram просит подождать %s с", exc.retry_after)
                break
            except Exception as exc:
                logger.warning("Уведомление %s не доставлено: %s", notification_id, exc)
                await _mark(notification_id, delivered=False, error=str(exc))
    finally:
        await bot.session.close()

    if sent:
        logger.info("Отправлено в Telegram: %s", sent)
    return sent


async def _mark_trigger(payload: dict | None) -> None:
    """Отметить доставку у срабатывания алерта, если оно известно.

    Уведомление и срабатывание — разные записи: первая живёт в очереди
    отправки, вторая в истории алерта. Без этой отметки история молчала
    бы о том, ушло ли сообщение.
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
    """Разослать уведомления подписанным браузерам.

    Пуш идёт тем же событиям, что и лента: показывать в панели, но не
    доводить до браузера, когда пользователь сам просил об этом, —
    полумера. Устройства, которые push-сервис объявил недействующими,
    удаляются сразу: повторять отправку по ним бессмысленно, а
    накапливаться они будут при каждой смене браузера.
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
            # Значения снимаем заранее: после возможного отката объекты
            # протухнут, а обращение к их полям упадёт.
            devices = [(t.id, t.endpoint, t.p256dh, t.auth) for t in targets]

        payload = {
            "title": title,
            "body": body,
            # Тег склеивает повторы одного события в одну карточку.
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
        logger.info("Отправлено веб-пушем: %s", sent)
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
    """Отметить, что попытка была.

    Повторять неудачную отправку не станем: пуш ценен свежестью, а
    очередь из вчерашних уведомлений — это уже не уведомления.
    """
    async with session_scope() as session:
        notification = await session.get(Notification, notification_id)
        if notification is None:
            return
        notification.delivered_push = True
        await session.commit()
