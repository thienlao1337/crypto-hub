"""Доставка уведомлений в Telegram.

Отдельно от записи события: сработавший алерт попадает в ленту сразу, а
в чат уходит следующим проходом. Если бот недоступен или пользователь
заблокировал его, событие не теряется и не блокирует движок алертов.

Кому что разрешено, здесь не решается: уведомление с выключенным
Telegram помечается доставленным ещё при записи и в очередь не попадает.
"""

import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import AlertTrigger, Notification, User

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
