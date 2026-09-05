"""Поддельная биржа для тестов сервисного слоя.

Позволяет проверять логику, не выходя в сеть и не имея ключей.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.exchanges.base import (
    BalanceEntry,
    ExchangeError,
    KeyCheck,
    MarketInfo,
    OhlcvBar,
    TickerInfo,
    TradeInfo,
)


class FakeAdapter:
    """Адаптер с заранее заданными ответами."""

    def __init__(
        self,
        *,
        key_check: KeyCheck | None = None,
        balances: list[BalanceEntry] | None = None,
        markets: list[MarketInfo] | None = None,
        tickers: list[TickerInfo] | None = None,
        trades: list[TradeInfo] | None = None,
        bars: list[OhlcvBar] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.key_check = key_check or KeyCheck(is_valid=True, can_trade=False, permissions_known=True)
        self.balances = balances or []
        self.markets = markets or []
        self.tickers = tickers or []
        self.trades = trades or []
        self.bars = bars or []
        self.raise_on = raise_on
        self.closed = False
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.raise_on == name:
            raise ExchangeError(f"Отказ на {name} (подстроено тестом)")

    async def check_key(self) -> KeyCheck:
        self._record("check_key")
        return self.key_check

    async def fetch_balances(self) -> list[BalanceEntry]:
        self._record("fetch_balances")
        return self.balances

    async def fetch_markets(self) -> list[MarketInfo]:
        self._record("fetch_markets")
        return self.markets

    async def fetch_tickers(self, symbols=None) -> list[TickerInfo]:
        self._record("fetch_tickers")
        if symbols is None:
            return self.tickers
        wanted = set(symbols)
        return [t for t in self.tickers if t.symbol in wanted]

    async def fetch_ohlcv(self, symbol, timeframe, *, since=None, limit=500) -> list[OhlcvBar]:
        self._record("fetch_ohlcv")
        return self.bars

    async def fetch_my_trades(self, symbol, *, since=None, limit=500) -> list[TradeInfo]:
        self._record("fetch_my_trades")
        return [t for t in self.trades if t.symbol == symbol]

    async def close(self) -> None:
        self.closed = True


def factory_for(adapter: FakeAdapter):
    """Фабрика, всегда отдающая один и тот же поддельный адаптер."""

    def _factory(exchange_code, api_key, api_secret, testnet):
        adapter.last_args = (exchange_code, api_key, api_secret, testnet)
        return adapter

    return _factory


def balance(asset: str, total: str, free: str | None = None) -> BalanceEntry:
    total_dec = Decimal(total)
    free_dec = Decimal(free) if free is not None else total_dec
    return BalanceEntry(
        asset=asset,
        free=free_dec,
        locked=total_dec - free_dec,
        total=total_dec,
    )


def market(symbol: str, base: str, quote: str) -> MarketInfo:
    return MarketInfo(
        symbol=symbol,
        raw_symbol=symbol.replace("/", ""),
        base=base,
        quote=quote,
        min_amount=Decimal("0.0001"),
        tick_size=Decimal("0.01"),
    )


def ticker(symbol: str, last: str, change_pct: str = "0") -> TickerInfo:
    return TickerInfo(
        symbol=symbol,
        last=Decimal(last),
        bid=Decimal(last),
        ask=Decimal(last),
        change_24h_pct=Decimal(change_pct),
    )


def trade(
    external_id: str,
    symbol: str,
    side: str,
    price: str,
    amount: str,
    *,
    executed_at: datetime | None = None,
) -> TradeInfo:
    price_dec = Decimal(price)
    amount_dec = Decimal(amount)
    return TradeInfo(
        external_id=external_id,
        symbol=symbol,
        side=side,
        price=price_dec,
        amount=amount_dec,
        cost=price_dec * amount_dec,
        executed_at=executed_at or datetime.now(timezone.utc),
    )
