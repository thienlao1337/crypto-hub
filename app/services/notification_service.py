"""Web panel notification feed.

Telegram delivery lives separately: here we only record the event. That way a triggered
alert isn't lost even if the bot is unavailable at that moment.

Where a notification goes is decided once - in dispatch(), when it is recorded.
Spreading that decision across the senders isn't an option: then "disabled" in the
settings would mean different things in the feed and in the bot.
"""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, NotificationSetting, User

KIND_ALERT = "alert"
KIND_SIGNAL = "signal"
KIND_SYSTEM = "system"

CHANNEL_WEB = "web"
CHANNEL_TELEGRAM = "telegram"

# The order also defines the column order on the settings page.
CHANNELS = (CHANNEL_WEB, CHANNEL_TELEGRAM)
EVENT_KINDS = (KIND_ALERT, KIND_SIGNAL, KIND_SYSTEM)

EVENT_TITLES = {
    KIND_ALERT: "Сработавшие алерты",
    KIND_SIGNAL: "Торговые сигналы",
    KIND_SYSTEM: "Системные сообщения",
}
EVENT_HINTS = {
    KIND_ALERT: "Цена дошла до уровня, изменилась на заданный процент, RSI пересёк порог.",
    KIND_SIGNAL: "Пересечения EMA и уровни RSI по парам из списка отслеживания.",
    KIND_SYSTEM: "Ключ биржи перестал работать, стратегия остановилась по лимиту.",
}

CHANNEL_TITLES = {
    CHANNEL_WEB: "В панели",
    CHANNEL_TELEGRAM: "В Telegram",
}


async def dispatch(
    session: AsyncSession,
    *,
    user_id: int,
    kind: str,
    title: str,
    body: str,
    payload: dict | None = None,
    web: bool = True,
    telegram: bool = True,
) -> Notification | None:
    """Record a notification taking the user's settings into account.

    web and telegram are permissions from the source side: the alert's own checkboxes.
    User settings can only narrow them. A single alert can't enable a channel disabled
    in the settings - otherwise "don't post to Telegram" would stop meaning anything.

    If no channel is left, no row is created at all: a notification nobody can see is
    junk in the table.
    """
    show_web = web and await is_enabled(session, user_id, kind, CHANNEL_WEB)
    send_telegram = telegram and await is_enabled(session, user_id, kind, CHANNEL_TELEGRAM)

    if not show_web and not send_telegram:
        return None

    return await push(
        session,
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        payload=payload,
        show_web=show_web,
        send_telegram=send_telegram,
    )


async def push(
    session: AsyncSession,
    *,
    user_id: int,
    kind: str,
    title: str,
    body: str,
    payload: dict | None = None,
    show_web: bool = True,
    send_telegram: bool = True,
) -> Notification:
    """Record a notification ignoring the settings.

    A direct call is appropriate for service messages the user can't turn off. Events
    from the spec - alerts and signals - need dispatch().
    """
    notification = Notification(
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        payload=payload,
        show_web=show_web,
        # A disabled channel is marked delivered right away: the send queue
        # shouldn't have to work out who is allowed what. Web push is tied to
        # the feed - what isn't shown in the panel isn't pushed either.
        delivered_telegram=not send_telegram,
        delivered_push=not show_web,
    )
    session.add(notification)
    await session.flush()
    return notification


async def recent(
    session: AsyncSession, user: User, *, limit: int = 30
) -> list[Notification]:
    result = await session.execute(
        select(Notification)
        .where(Notification.user_id == user.id, Notification.show_web.is_(True))
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def unread_count(session: AsyncSession, user: User) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == user.id,
            Notification.show_web.is_(True),
            Notification.is_read.is_(False),
        )
    )
    return int(result.scalar_one())


async def mark_all_read(session: AsyncSession, user: User) -> int:
    result = await session.execute(
        update(Notification)
        .where(
            Notification.user_id == user.id,
            Notification.show_web.is_(True),
            Notification.is_read.is_(False),
        )
        .values(is_read=True)
    )
    return int(result.rowcount or 0)


async def settings_matrix(session: AsyncSession, user: User) -> dict[tuple[str, str], bool]:
    """Current state of all toggles for the settings page."""
    rows = await session.execute(
        select(
            NotificationSetting.event_type,
            NotificationSetting.channel,
            NotificationSetting.is_enabled,
        ).where(NotificationSetting.user_id == user.id)
    )
    stored = {(event, channel): enabled for event, channel, enabled in rows}

    return {
        (event, channel): stored.get((event, channel), True)
        for event in EVENT_KINDS
        for channel in CHANNELS
    }


async def is_enabled(
    session: AsyncSession, user_id: int, event_type: str, channel: str
) -> bool:
    """Whether a channel is enabled for an event.

    A missing setting means "enabled": a user who hasn't configured anything should get
    notifications, not silence.
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
