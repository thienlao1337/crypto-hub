"""Telegram-бот: зеркало веб-функций.

Отдельный процесс, как и worker. Бот и панель работают с одними и теми
же сервисами, поэтому цифры в чате и на сайте не расходятся.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot import middlewares
from app.bot.handlers import common, data
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")
settings = get_settings()


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()

    # Порядок важен: сессия должна появиться раньше, чем её попросит
    # слой пользователя, а перехват ошибок — обернуть оба.
    dispatcher.message.middleware(middlewares.ErrorsMiddleware())
    dispatcher.message.middleware(middlewares.DbSessionMiddleware())
    dispatcher.message.middleware(middlewares.UserMiddleware())

    # Обработчик по умолчанию из common ловит всё подряд, поэтому
    # подключается последним.
    dispatcher.include_router(data.router)
    dispatcher.include_router(common.router)
    return dispatcher


async def main() -> None:
    if not settings.bot_token:
        logger.error(
            "BOT_TOKEN не задан — бот не работает. "
            "Получите токен у @BotFather и укажите его в .env, "
            "затем перезапустите: docker compose restart bot"
        )
        # Просто выйти нельзя: docker перезапустит контейнер, и журнал
        # забьётся одной и той же строкой. Останавливаемся и ждём.
        await asyncio.Event().wait()
        return

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = build_dispatcher()

    me = await bot.get_me()
    logger.info("Бот @%s запущен", me.username)

    try:
        # Накопившиеся за простой сообщения пропускаем: отвечать на
        # команду недельной давности незачем.
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
