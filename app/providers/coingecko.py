"""Market-wide metrics from CoinGecko.

Without a key the public tier has a strict limit, so the request is made rarely and its
result is stored in global_stats.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coingecko.com/api/v3"
TIMEOUT = 15.0


@dataclass(frozen=True)
class GlobalMarket:
    total_market_cap_usd: Decimal | None
    total_volume_24h_usd: Decimal | None
    market_cap_change_24h_pct: Decimal | None
    btc_dominance: Decimal | None
    eth_dominance: Decimal | None


def _headers() -> dict[str, str]:
    key = get_settings().coingecko_api_key
    # The demo key goes in its own header; without a key we send a plain request.
    return {"x-cg-demo-api-key": key} if key else {}


async def fetch_global() -> GlobalMarket | None:
    """Market cap and dominance. None - the source is unavailable."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{BASE_URL}/global", headers=_headers())
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("CoinGecko unavailable: %s", exc)
        return None

    data = payload.get("data") or {}
    dominance = data.get("market_cap_percentage") or {}

    return GlobalMarket(
        total_market_cap_usd=_decimal((data.get("total_market_cap") or {}).get("usd")),
        total_volume_24h_usd=_decimal((data.get("total_volume") or {}).get("usd")),
        market_cap_change_24h_pct=_decimal(data.get("market_cap_change_percentage_24h_usd")),
        btc_dominance=_decimal(dominance.get("btc")),
        eth_dominance=_decimal(dominance.get("eth")),
    )


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
