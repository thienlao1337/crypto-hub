"""Адаптеры P2P-площадок."""

from app.exchanges.p2p.base import (
    AdInfo,
    BoardEntry,
    OrderInfo,
    P2PAccess,
    P2PAccessDenied,
    P2PAdapter,
    P2PError,
    P2PResponseError,
)
from app.exchanges.p2p.binance import BinanceP2PAdapter
from app.exchanges.p2p.bybit import BybitP2PAdapter

SUPPORTED = ("bybit", "binance")

_ADAPTERS = {
    "bybit": BybitP2PAdapter,
    "binance": BinanceP2PAdapter,
}


def build_adapter(
    exchange_code: str, api_key: str, api_secret: str, testnet: bool = False
) -> P2PAdapter:
    """Адаптер площадки по её коду."""
    factory = _ADAPTERS.get(exchange_code)
    if factory is None:
        raise P2PError(f"P2P для биржи {exchange_code} не поддерживается.")
    return factory(api_key, api_secret, testnet=testnet)


__all__ = [
    "AdInfo",
    "BoardEntry",
    "OrderInfo",
    "P2PAccess",
    "P2PAccessDenied",
    "P2PAdapter",
    "P2PError",
    "P2PResponseError",
    "BinanceP2PAdapter",
    "BybitP2PAdapter",
    "SUPPORTED",
    "build_adapter",
]
