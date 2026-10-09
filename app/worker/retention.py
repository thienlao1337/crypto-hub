"""Cleanup of old data.

The app writes continuously and until now never deleted anything. Candles are the most
noticeable: six timeframes per watched pair is about two thousand rows a day per pair,
i.e. several gigabytes a year across thirty pairs. Yet only the latest few hundred are
ever read: neither indicators nor the chart need more.

Candles are capped by count, not age, because a week produces ten thousand one-minute
candles but only seven daily ones. An age limit would mean either junk in one timeframe
or an empty chart in another.

Other tables are cleaned by age: what matters there is how old an event is, not how many
there are.
"""

import logging

from sqlalchemy import text

from app.config import get_settings
from app.db import session_scope

logger = logging.getLogger(__name__)

# Candles: how many of the latest to keep per pair and timeframe.
KEEP_CANDLES_SQL = text(
    """
    DELETE FROM candles
    WHERE id IN (
        SELECT id FROM (
            SELECT id, row_number() OVER (
                PARTITION BY market_id, timeframe_id ORDER BY open_time DESC
            ) AS position
            FROM candles
        ) ranked
        WHERE ranked.position > :keep
    )
    """
)

# Tables cleaned by age: name, time column, setting.
AGED = (
    ("login_events", "created_at", "login_events_keep_days"),
    ("notifications", "created_at", "notifications_keep_days"),
    ("global_stats", "captured_at", "global_stats_keep_days"),
    # The rule is recalculated once a minute. Consecutive repeats aren't
    # written, but in a volatile market there are still many entries.
    ("p2p_price_events", "created_at", "p2p_events_keep_days"),
)


async def cleanup() -> dict[str, int]:
    """Remove what nobody will read anymore. Returns what was deleted."""
    settings = get_settings()
    removed: dict[str, int] = {}

    try:
        async with session_scope() as session:
            result = await session.execute(
                KEEP_CANDLES_SQL, {"keep": settings.candles_keep_per_series}
            )
            removed["candles"] = int(result.rowcount or 0)

            for table, column, option in AGED:
                days = getattr(settings, option)
                if days <= 0:
                    # Zero means "don't touch": the client may want to keep the
                    # login log longer than our default.
                    continue
                deleted = await session.execute(
                    text(
                        f"DELETE FROM {table} "
                        f"WHERE {column} < now() - make_interval(days => :days)"
                    ),
                    {"days": days},
                )
                removed[table] = int(deleted.rowcount or 0)

            await session.commit()
    except Exception:
        logger.exception("Old data cleanup failed")
        return {}

    total = sum(removed.values())
    if total:
        logger.info(
            "Rows removed: %s",
            ", ".join(f"{table} {count}" for table, count in removed.items() if count),
        )
    return removed
