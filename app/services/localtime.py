"""Перевод времени в пояс пользователя.

Отдельным модулем, потому что нужен и панели, и боту: расхождение времени
между чатом и сайтом читалось бы как ошибка в данных. Хранится всё в UTC,
меняется только отображение.
"""

from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "UTC"


@lru_cache(maxsize=64)
def zone(name: str) -> ZoneInfo:
    """Пояс по имени. Неизвестное имя не должно ронять страницу."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def in_zone(value: datetime, name: str | None) -> datetime:
    """Перевести момент в пояс пользователя.

    Наивное время считаем UTC: всё, что пишет приложение, tz-aware, но
    драйвер базы в отдельных случаях отдаёт время без пояса, и молча
    сдвигать его на местный было бы хуже всего.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone(name or DEFAULT_TIMEZONE))


def moment(value: datetime | None, user, fmt: str = "%d.%m %H:%M") -> str:
    """Отформатировать момент в поясе пользователя."""
    if value is None:
        return "—"
    return in_zone(value, getattr(user, "timezone", None)).strftime(fmt)


def to_utc(value: datetime, name: str | None) -> datetime:
    """Обратный перевод: местное время из формы — в UTC для хранения.

    Поле datetime-local в браузере отдаёт время без пояса, и оно местное
    для пользователя, а не для сервера. Считать его UTC значило бы
    промахнуться ровно на разницу поясов.
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=zone(name or DEFAULT_TIMEZONE)).astimezone(timezone.utc)
