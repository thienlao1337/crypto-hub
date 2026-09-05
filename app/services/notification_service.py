"""Лента уведомлений веб-панели.

Доставка в Telegram живёт отдельно: здесь только запись события. Так
сработавший алерт не теряется, даже если бот в этот момент недоступен.
"""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, NotificationSetting, User

KIND_ALERT = "alert"
KIND_SIGNAL = "signal"
KIND_SYSTEM = "system"

CHANNEL_WEB = "web"
CHANNEL_TELEGRAM = "telegram"


async def push(
    session: AsyncSession,
    *,
    user_id: int,
    kind: str,
    title: str,
    body: str,
    payload: dict | None = None,
) -> Notification:
    notification = Notification(
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        payload=payload,
    )
    session.add(notification)
    await session.flush()
    return notification


async def recent(
    session: AsyncSession, user: User, *, limit: int = 30
) -> list[Notification]:
    result = await session.execute(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def unread_count(session: AsyncSession, user: User) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.is_read.is_(False))
    )
    return int(result.scalar_one())


async def mark_all_read(session: AsyncSession, user: User) -> int:
    result = await session.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    return int(result.rowcount or 0)


async def is_enabled(
    session: AsyncSession, user_id: int, event_type: str, channel: str
) -> bool:
    """Включён ли канал для события.

    Отсутствие настройки означает «включено»: пользователь, который
    ничего не настраивал, должен получать уведомления, а не тишину.
    """
    result = await session.execute(
        select(NotificationSetting.is_enabled).where(
            NotificationSetting.user_id == user_id,
            NotificationSetting.event_type == event_type,
            NotificationSetting.channel == channel,
        )
    )
    value = result.scalar_one_or_none()
    return True if value is None else bool(value)


async def set_enabled(
    session: AsyncSession, user_id: int, event_type: str, channel: str, enabled: bool
) -> None:
    result = await session.execute(
        select(NotificationSetting).where(
            NotificationSetting.user_id == user_id,
            NotificationSetting.event_type == event_type,
            NotificationSetting.channel == channel,
        )
    )
    setting = result.scalar_one_or_none()
    if setting is None:
        setting = NotificationSetting(
            user_id=user_id, event_type=event_type, channel=channel
        )
        session.add(setting)
    setting.is_enabled = enabled
    await session.flush()
