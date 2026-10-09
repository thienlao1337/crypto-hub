"""Bot middlewares: DB session, user, error handling."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.db import session_scope
from app.services import user_service

logger = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    """A dedicated session per message.

    One session for the whole process won't do: an error in one handler would leave it
    unusable for every following one.
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
    """Injects the panel user by Telegram id.

    If not found, data gets None and the handler decides what to say. We can't reject
    here: the linking command has to work for an unlinked account too.
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
    """One error handler for everything: the bot must never go silent.

    The user gets a clear message; the details go to the server log.
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
            logger.exception("Error in bot handler")
            if isinstance(event, Message):
                await event.answer(
                    "Что-то пошло не так. Ошибка записана, попробуйте ещё раз."
                )
            return None
