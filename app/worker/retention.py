"""Уборка старых данных.

Приложение пишет непрерывно и до сих пор ничего не удаляло. Заметнее
всего свечи: шесть таймфреймов на каждую отслеживаемую пару — это около
двух тысяч строк в сутки на пару, то есть несколько гигабайт в год на
тридцати парах. Читаются при этом всегда только последние несколько сотен:
и индикаторам, и графику больше не нужно.

Свечи ограничиваются количеством, а не возрастом, потому что минутных за
неделю набегает десять тысяч, а дневных — семь. Ограничение по возрасту
означало бы либо мусор в одном таймфрейме, либо пустой график в другом.

Остальные таблицы чистятся по возрасту: там важна давность события, а не
их число.
"""

import logging

from sqlalchemy import text

from app.config import get_settings
from app.db import session_scope

logger = logging.getLogger(__name__)

# Свечи: сколько последних оставлять на каждую пару и таймфрейм.
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

# Таблицы, которые чистятся по возрасту: имя, столбец времени, настройка.
AGED = (
    ("login_events", "created_at", "login_events_keep_days"),
    ("notifications", "created_at", "notifications_keep_days"),
    ("global_stats", "captured_at", "global_stats_keep_days"),
    # Правило пересчитывается раз в минуту. Повторы подряд не пишутся, но
    # на подвижном рынке записей всё равно много.
    ("p2p_price_events", "created_at", "p2p_events_keep_days"),
)


async def cleanup() -> dict[str, int]:
    """Убрать то, что уже никто не прочитает. Возвращает удалённое."""
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
                    # Ноль означает «не трогать»: клиент может захотеть
                    # хранить журнал входов дольше нашего умолчания.
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
        logger.exception("Уборка старых данных не удалась")
        return {}

    total = sum(removed.values())
    if total:
        logger.info(
            "Убрано строк: %s",
            ", ".join(f"{table} {count}" for table, count in removed.items() if count),
        )
    return removed
