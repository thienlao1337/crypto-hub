"""Общий интерфейс адаптера биржи.

Сервисы работают только с этими типами и не знают, что под капотом
ccxt. Это же делает их тестируемыми: в тестах подставляется поддельный
адаптер, а не поднимается сеть.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

SIDE_BUY = "buy"
SIDE_SELL = "sell"


class ExchangeError(Exception):
    """Базовая ошибка работы с биржей."""


class ExchangeAuthError(ExchangeError):
    """Ключ недействителен, отозван или не имеет нужных прав."""


class ExchangeRateLimited(ExchangeError):
    """Биржа попросила сбавить темп."""


class ExchangeUnavailable(ExchangeError):
    """Сеть или биржа временно недоступны — имеет смысл повторить."""


@dataclass(frozen=True)
class BalanceEntry:
    asset: str
    free: Decimal
    locked: Decimal
    total: Decimal


@dataclass(frozen=True)
class MarketInfo:
    symbol: str  # унифицированный вид: BTC/USDT
    raw_symbol: str  # как у биржи: BTCUSDT
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
    """Ответ биржи на выставленный ордер."""

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
    """Результат проверки ключа у самой биржи.

    can_trade заполняется только когда биржа явно подтвердила право на
    торговлю. Если выяснить не удалось — остаётся False: право на
    реальные сделки не выдаётся по догадке.
    """

    is_valid: bool
    can_trade: bool = False
    permissions_known: bool = False
    error: str | None = None


class ExchangeAdapter(Protocol):
    """То, что сервисы вправе спросить у биржи."""

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
    """Перевести число из ccxt в Decimal без потери на float.

    ccxt отдаёт float; Decimal(0.1) даёт 0.1000000000000000055…, поэтому
    идём через строку.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
