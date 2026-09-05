"""Промежуточные слои бота: сессия БД, пользователь, обработка ошибок."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.db import session_scope
from app.services import user_service

logger = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    """Своя сессия на каждое сообщение.

    Одна сессия на весь процесс не годится: ошибка в одном обработчике
    оставила бы её в непригодном состоянии для всех следующих.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with session_scope() as session:
            data["session"] = session
            return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Подставляет пользователя панели по идентификатору Telegram.

    Не найден — в data приходит None, и обработчик сам решает, что
    сказать. Отсекать здесь нельзя: команда привязки должна работать и
    для непривязанного аккаунта.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data["session"]
        telegram_user = data.get("event_from_user")

        data["user"] = (
            await user_service.get_by_telegram_id(session, telegram_user.id)
            if telegram_user is not None
            else None
        )
        return await handler(event, data)


class ErrorsMiddleware(BaseMiddleware):
    """Один обработчик ошибок на всех: бот не должен молча умолкать.

    Пользователю уходит понятная фраза, подробности — в журнал сервера.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("Ошибка в обработчике бота")
            if isinstance(event, Message):
                await event.answer(
                    "Что-то пошло не так. Ошибка записана, попробуйте ещё раз."
                )
            return None
