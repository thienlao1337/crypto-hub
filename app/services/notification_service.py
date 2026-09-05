"""Лента уведомлений веб-панели.

Доставка в Telegram живёт отдельно: здесь только запись события. Так
сработавший алерт не теряется, даже если бот в этот момент недоступен.

Куда уведомление пойдёт, решается один раз — в dispatch(), при записи.
Разносить это решение по местам отправки нельзя: тогда «выключено» в
настройках означало бы разное в ленте и в боте.
"""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, NotificationSetting, User

KIND_ALERT = "alert"
KIND_SIGNAL = "signal"
KIND_SYSTEM = "system"

CHANNEL_WEB = "web"
CHANNEL_TELEGRAM = "telegram"

# Порядок задаёт и порядок колонок на странице настроек.
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
    """Записать уведомление с учётом настроек пользователя.

    web и telegram — разрешения со стороны источника: галочки самого
    алерта. Настройки пользователя их только сужают. Включить канал,
    выключенный в настройках, отдельный алерт не может — иначе «не
    писать в Telegram» перестало бы что-либо значить.

    Если не остаётся ни одного канала, запись не создаётся вовсе:
    уведомление, которое некому показать, — мусор в таблице.
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
    """Записать уведомление без оглядки на настройки.

    Прямой вызов уместен для служебных сообщений, которые пользователь
    отключить не может. Для событий из ТЗ — алертов и сигналов — нужен
    dispatch().
    """
    notification = Notification(
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        payload=payload,
        show_web=show_web,
        # Отключённый канал помечаем доставленным сразу: очередь отправки
        # не должна разбираться, кому что разрешено.
        delivered_telegram=not send_telegram,
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
    """Текущее состояние всех переключателей для страницы настроек."""
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
