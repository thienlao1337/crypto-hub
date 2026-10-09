"""Common exchange adapter interface.

Services work only with these types and don't know ccxt is under the hood. That also
makes them testable: tests plug in a fake adapter instead of hitting the network.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

SIDE_BUY = "buy"
SIDE_SELL = "sell"


class ExchangeError(Exception):
    """Base error for exchange operations."""


class ExchangeAuthError(ExchangeError):
    """The key is invalid, revoked or lacks the required permissions."""


class ExchangeRateLimited(ExchangeError):
    """The exchange asked us to slow down."""


class ExchangeUnavailable(ExchangeError):
    """Network or exchange temporarily unavailable - worth retrying."""


@dataclass(frozen=True)
class BalanceEntry:
    asset: str
    free: Decimal
    locked: Decimal
    total: Decimal


@dataclass(frozen=True)
class MarketInfo:
    symbol: str  # unified form: BTC/USDT
    raw_symbol: str  # as the exchange writes it: BTCUSDT
    base: str
    quote: str
    market_type: str = "spot"
    price_precision: int | None = None
    amount_precision: int | None = None
    min_amount: Decimal | None = None
    tick_size: Decimal | None = None


@dataclass(frozen=True)
class TickerInfo:
    symbol: str
    last: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    high_24h: Decimal | None = None
    low_24h: Decimal | None = None
    volume_24h: Decimal | None = None
    quote_volume_24h: Decimal | None = None
    change_24h_pct: Decimal | None = None


@dataclass(frozen=True)
class OhlcvBar:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class TradeInfo:
    external_id: str
    symbol: str
    side: str
    price: Decimal
    amount: Decimal
    cost: Decimal
    executed_at: datetime
    order_id: str | None = None
    fee: Decimal | None = None
    fee_asset: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OrderResult:
    """Exchange response to a placed order."""

    external_id: str
    symbol: str
    side: str
    amount: Decimal
    price: Decimal | None
    status: str
    filled: Decimal = Decimal(0)
    average_price: Decimal | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class KeyCheck:
    """Result of checking the key with the exchange itself.

    can_trade is set only when the exchange explicitly confirmed trading permission. If
    it couldn't be determined it stays False: live trading permission is never granted
    on a guess.
    """

    is_valid: bool
    can_trade: bool = False
    permissions_known: bool = False
    error: str | None = None


class ExchangeAdapter(Protocol):
    """What services are allowed to ask the exchange."""

    async def check_key(self) -> KeyCheck: ...

    async def fetch_balances(self) -> list[BalanceEntry]: ...

    async def fetch_markets(self) -> list[MarketInfo]: ...

    async def fetch_tickers(self, symbols: list[str] | None = None) -> list[TickerInfo]: ...

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[OhlcvBar]: ...

    async def fetch_my_trades(
        self,
        symbol: str,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[TradeInfo]: ...

    async def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
    ) -> OrderResult: ...

    async def close(self) -> None: ...


def to_decimal(value) -> Decimal | None:
    """Convert a number from ccxt to Decimal without float loss.

    ccxt returns float; Decimal(0.1) gives 0.1000000000000000055..., so we go through a
    string.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
