"""Fear & Greed index (alternative.me).

The source only returns the current value, so we accumulate history ourselves - as
snapshots in global_stats. No key required.
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

URL = "https://api.alternative.me/fng/"
TIMEOUT = 10.0

# The source returns the label in English; we translate it here so the UI
# doesn't have to deal with a dictionary.
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
    """Current index value. None if the source is unavailable.

    A widget without data is better than a failed job: the other dashboard metrics don't
    depend on it.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(URL, params={"limit": 1})
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Fear & Greed index unavailable: %s", exc)
        return None

    rows = payload.get("data") or []
    if not rows:
        return None

    row = rows[0]
    try:
        value = int(row["value"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Unexpected index response: %s", row)
        return None

    raw_label = str(row.get("value_classification") or "").strip().lower()
    return FearGreed(value=value, label=LABELS.get(raw_label, raw_label or "—"))
