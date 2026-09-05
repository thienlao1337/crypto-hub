"""Индекс страха и жадности (alternative.me).

Источник отдаёт только текущее значение, поэтому историю мы копим сами —
снимками в global_stats. Ключ не требуется.
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

URL = "https://api.alternative.me/fng/"
TIMEOUT = 10.0

# Источник отдаёт метку по-английски; переводим здесь, чтобы интерфейс
# не занимался словарём.
LABELS = {
    "extreme fear": "крайний страх",
    "fear": "страх",
    "neutral": "нейтрально",
    "greed": "жадность",
    "extreme greed": "крайняя жадность",
}


@dataclass(frozen=True)
class FearGreed:
    value: int
    label: str


async def fetch() -> FearGreed | None:
    """Текущее значение индекса. None — если источник недоступен.

    Виджет без данных лучше, чем упавшая задача: остальные показатели
    дашборда от этого не зависят.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(URL, params={"limit": 1})
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Индекс страха и жадности недоступен: %s", exc)
        return None

    rows = payload.get("data") or []
    if not rows:
        return None

    row = rows[0]
    try:
        value = int(row["value"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Неожиданный ответ индекса: %s", row)
        return None

    raw_label = str(row.get("value_classification") or "").strip().lower()
    return FearGreed(value=value, label=LABELS.get(raw_label, raw_label or "—"))
