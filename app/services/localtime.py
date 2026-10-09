"""Converting time to the user's time zone.

A separate module because both the panel and the bot need it: a time mismatch between
the chat and the site would read as a data error. Everything is stored in UTC; only the
display changes.
"""

from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "UTC"


@lru_cache(maxsize=64)
def zone(name: str) -> ZoneInfo:
    """Time zone by name. An unknown name must not break the page."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def in_zone(value: datetime, name: str | None) -> datetime:
    """Convert a moment to the user's time zone.

    Naive time is treated as UTC: everything the app writes is tz-aware, but in some
    cases the database driver returns time without a zone, and silently shifting it to
    local time would be the worst option.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone(name or DEFAULT_TIMEZONE))


def moment(value: datetime | None, user, fmt: str = "%d.%m %H:%M") -> str:
    """Format a moment in the user's time zone."""
    if value is None:
        return "—"
    return in_zone(value, getattr(user, "timezone", None)).strftime(fmt)


def to_utc(value: datetime, name: str | None) -> datetime:
    """The reverse conversion: local time from a form to UTC for storage.

    A datetime-local field in the browser returns time without a zone, and it's local to
    the user, not the server. Treating it as UTC would be off by exactly the zone
    difference.
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=zone(name or DEFAULT_TIMEZONE)).astimezone(timezone.utc)
