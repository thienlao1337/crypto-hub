"""Telegram bot: a mirror of the web features.

A separate process, like the worker. The bot and the panel use the same services, so the
numbers in chat and on the site never diverge.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot import middlewares
from app.bot.handlers import common, data
from app.config import get_settings, verify_deployment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")
settings = get_settings()


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()

    # Order matters: the session must exist before the user layer asks for it,
    # and error handling must wrap both.
    dispatcher.message.middleware(middlewares.ErrorsMiddleware())
    dispatcher.message.middleware(middlewares.DbSessionMiddleware())
    dispatcher.message.middleware(middlewares.UserMiddleware())

    # The default handler from common catches everything, so it is registered
    # last.
    dispatcher.include_router(data.router)
    dispatcher.include_router(common.router)
    return dispatcher


async def main() -> None:
    verify_deployment(settings)

    if not settings.bot_token:
        logger.error(
            "BOT_TOKEN is not set - the bot is not running. "
            "Get a token from @BotFather, put it in .env, "
            "then restart: docker compose restart bot"
        )
        # We can't just exit: docker would restart the container and the log
        # would fill up with the same line. Stop and wait instead.
        await asyncio.Event().wait()
        return

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = build_dispatcher()

    me = await bot.get_me()
    logger.info("Bot @%s started", me.username)

    try:
        # Skip messages that piled up during downtime: there's no point
        # answering a week-old command.
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
