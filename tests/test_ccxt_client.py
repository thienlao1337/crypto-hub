"""Exchange adapter: placing orders and parsing errors.

This is the only path real money goes through, and the only one that can't be exercised
without keys. Hence a fake exchange here: it won't verify that Bybit understands us, but
it verifies what we send and what we show the user when the exchange refuses.
"""

from decimal import Decimal

import ccxt.async_support as real_ccxt
import pytest

from app.exchanges import ccxt_client
from app.exchanges.base import (
    ExchangeAuthError,
    ExchangeError,
    ExchangeRateLimited,
    ExchangeUnavailable,
)

SYMBOL = "BTC/USDT"

# Bybit's BTC lot step: the amount is rounded down to thousandths.
LOT_STEP = Decimal("0.001")


class FakeExchange:
    """An exchange that records what it was sent."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.markets: dict = {}
        self.orders: list[tuple] = []
        self.sandbox = False
        self.closed = False
        self.raise_on_order: Exception | None = None
        self.load_calls = 0

    async def load_markets(self):
        self.load_calls += 1
        self.markets = {SYMBOL: {"spot": True, "active": True}}
        return self.markets

    def amount_to_precision(self, symbol, amount):
        """The same thing ccxt does: truncation to the lot step."""
        if not self.markets:
            raise real_ccxt.ExchangeError("markets not loaded")
        step = Decimal(str(LOT_STEP))
        return str((Decimal(str(amount)) // step) * step)

    async def create_order(self, symbol, order_type, side, amount):
        if self.raise_on_order is not None:
            raise self.raise_on_order
        self.orders.append((symbol, order_type, side, amount))
        return {
            "id": "order-1",
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": None,
            "status": "closed",
            "filled": amount,
            "average": 80000.0,
            "info": {"raw": "ok"},
        }

    async def fetch_balance(self):
        return {"total": {}}

    def set_sandbox_mode(self, flag):
        self.sandbox = flag

    async def close(self):
        self.closed = True


class FakeCcxtModule:
    """The ccxt module with a substituted exchange factory.

    It's the module that gets replaced, not self._client: the adapter creates the client
    in its constructor, and replacing it afterwards wouldn't test the settings it was
    created with.
    """

    def __init__(self, exchange: FakeExchange) -> None:
        self._exchange = exchange
        self.bybit = lambda config: self._configure(config)
        self.binance = self.bybit

    def _configure(self, config):
        self._exchange.config = config
        return self._exchange

    def __getattr__(self, name):
        # Real exception classes are used: the adapter catches exactly those.
        return getattr(real_ccxt, name)


@pytest.fixture
def exchange(monkeypatch) -> FakeExchange:
    fake = FakeExchange({})
    monkeypatch.setattr(ccxt_client, "ccxt", FakeCcxtModule(fake))
    return fake


def adapter(**kwargs) -> ccxt_client.CcxtAdapter:
    return ccxt_client.CcxtAdapter("bybit", api_key="k", api_secret="s", **kwargs)


# --- Placing an order ---


async def test_amount_is_rounded_to_lot_step(exchange):
    """The exchange won't accept an amount from a deposit-share calculation as is.

    0.05358804425365755979124688685 is the result of a division, while the exchange only
    accepts multiples of its lot step.
    """
    async with adapter() as api:
        await api.create_market_order(
            SYMBOL, "buy", Decimal("0.05358804425365755979124688685")
        )

    symbol, order_type, side, amount = exchange.orders[0]
    assert (symbol, order_type, side) == (SYMBOL, "market", "buy")
    assert Decimal(str(amount)) == Decimal("0.053")


async def test_markets_are_loaded_before_rounding(exchange):
    """Without the instrument list ccxt doesn't know the lot step."""
    async with adapter() as api:
        await api.create_market_order(SYMBOL, "buy", Decimal("1"))

    assert exchange.load_calls >= 1


async def test_amount_below_lot_step_is_refused_before_exchange(exchange):
    """Sending zero after rounding is pointless and dangerous."""
    async with adapter() as api:
        with pytest.raises(ExchangeError) as info:
            await api.create_market_order(SYMBOL, "buy", Decimal("0.0004"))

    assert "шага лота" in str(info.value)
    assert exchange.orders == [], "до биржи дело доходить не должно"


async def test_result_carries_amount_accepted_by_exchange(exchange):
    async with adapter() as api:
        result = await api.create_market_order(SYMBOL, "buy", Decimal("0.0559"))

    assert result.amount == Decimal("0.055")
    assert result.external_id == "order-1"
    assert result.average_price == Decimal("80000")


async def test_unknown_side_rejected(exchange):
    async with adapter() as api:
        with pytest.raises(ExchangeError):
            await api.create_market_order(SYMBOL, "hodl", Decimal("1"))

    assert exchange.orders == []


async def test_testnet_switches_client_to_sandbox(exchange):
    async with adapter(testnet=True):
        pass

    assert exchange.sandbox is True


async def test_adapter_closes_session(exchange):
    async with adapter():
        pass

    assert exchange.closed is True


# --- Exchange errors on the outside ---


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (real_ccxt.AuthenticationError, ExchangeAuthError),
        (real_ccxt.PermissionDenied, ExchangeAuthError),
        (real_ccxt.RateLimitExceeded, ExchangeRateLimited),
        (real_ccxt.ExchangeNotAvailable, ExchangeUnavailable),
        (real_ccxt.InsufficientFunds, ExchangeError),
    ],
)
async def test_exchange_errors_are_translated(exchange, raised, expected):
    exchange.raise_on_order = raised("сырой текст ccxt")

    async with adapter() as api:
        with pytest.raises(expected):
            await api.create_market_order(SYMBOL, "buy", Decimal("1"))


async def test_raw_ccxt_text_does_not_reach_the_user(exchange):
    """The ccxt message contains the request URL together with the signature.

    It ends up in last_error and from there on screen, where it explains nothing but
    shows too much.
    """
    secret_looking = (
        "bybit GET https://api.bybit.com/v5/order?api_key=AAA&sign=deadbeef "
        '{"retCode":10004}'
    )
    exchange.raise_on_order = real_ccxt.AuthenticationError(secret_looking)

    async with adapter() as api:
        with pytest.raises(ExchangeAuthError) as info:
            await api.create_market_order(SYMBOL, "buy", Decimal("1"))

    message = str(info.value)
    assert "sign=" not in message
    assert "api_key" not in message
    assert message == "Биржа отклонила ключ."
