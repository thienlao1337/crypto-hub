"""Старт, справка и привязка аккаунта."""

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import User
from app.services import user_service

router = Router(name="common")
settings = get_settings()

HELP = (
    "<b>Crypto Hub</b>\n\n"
    "/portfolio — сводка по балансам\n"
    "/price BTC — котировка на обеих биржах\n"
    "/signals — свежие сигналы по отслеживаемым парам\n"
    "/alert BTC &gt; 70000 — создать алерт по цене\n"
    "/alerts — список алертов\n"
    "/unlink — отвязать этот чат от аккаунта\n\n"
    "<i>Данные и сигналы носят информационный характер. "
    "Это не финансовая рекомендация.</i>"
)


def link_hint() -> str:
    return (
        "Этот чат не привязан к аккаунту.\n\n"
        "Откройте в панели раздел «Безопасность», получите код привязки и "
        "отправьте его сюда одним сообщением.\n"
        f"Панель: {settings.public_url}"
    )


@router.message(CommandStart())
async def start(message: Message, user: User | None) -> None:
    if user is None:
        await message.answer(link_hint())
        return
    await message.answer(f"Здравствуйте! Чат привязан к {user.email}.\n\n{HELP}")


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("unlink"))
async def unlink(message: Message, session: AsyncSession, user: User | None) -> None:
    if user is None:
        await message.answer("Этот чат и так не привязан.")
        return

    user.telegram_id = None
    user.telegram_username = None
    await session.commit()
    await message.answer(
        "Чат отвязан. Уведомления сюда больше не придут. "
        "Чтобы вернуть — получите новый код в панели."
    )


@router.message(lambda message: (message.text or "").strip().isdigit())
async def link_by_code(message: Message, session: AsyncSession, user: User | None) -> None:
    """Числовое сообщение трактуем как код привязки.

    Отдельной командой это делать неудобно: код приходится копировать, и
    лишнее слово перед ним — частая причина «не работает».
    """
    if user is not None:
        await message.answer("Чат уже привязан. Отвязать — /unlink")
        return

    try:
        linked = await user_service.link_telegram(
            session,
            code=message.text.strip(),
            telegram_id=message.from_user.id,
            telegram_username=message.from_user.username,
        )
    except user_service.TelegramCodeInvalid as exc:
        await message.answer(str(exc))
        return

    await session.commit()
    await message.answer(f"Готово, чат привязан к {linked.email}.\n\n{HELP}")


@router.message()
async def fallback(message: Message, user: User | None) -> None:
    if user is None:
        await message.answer(link_hint())
        return
    await message.answer("Не понял команду.\n\n" + HELP)
