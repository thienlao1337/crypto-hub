"""Converter and trade calculator tests.

The numbers here are computed by hand: a calculator that lies about fees is worse than
none.
"""

from decimal import Decimal

import pytest
import pytest_asyncio

from app.models import Exchange, MarketTicker
from app.services import market_service, tools_service
from tests import fakes


# --- Input parsing ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", Decimal(1)),
        ("0.5", Decimal("0.5")),
        # A comma is a habit of the Russian layout, spaces get copied from the markup.
        ("1,5", Decimal("1.5")),
        (" 70 000,25 ", Decimal("70000.25")),
    ],
)
def test_parse_decimal(raw, expected):
    assert tools_service.parse_decimal(raw, "поле") == expected


@pytest.mark.parametrize("raw", ["", "   ", "абв", "1.2.3"])
def test_parse_decimal_rejects_nonsense(raw):
    with pytest.raises(tools_service.ToolsError):
        tools_service.parse_decimal(raw, "количество")


# --- Trade calculator ---


def test_long_profit_accounts_for_both_fees():
    """The fee is charged both on entry and exit.

    0.1 BTC: entry 100,000 (=10,000), exit 110,000 (=11,000).
    Fee 0.1%: 10 + 11 = 21. Gross 1000, net 979.
    """
    result = tools_service.calculate_trade(
        side="buy",
        amount=Decimal("0.1"),
        entry_price=Decimal(100_000),
        exit_price=Decimal(110_000),
        fee_pct=Decimal("0.1"),
    )

    assert result.entry_cost == Decimal(10_000)
    assert result.exit_proceeds == Decimal(11_000)
    assert result.entry_fee == Decimal(10)
    assert result.exit_fee == Decimal(11)
    assert result.gross_pnl == Decimal(1_000)
    assert result.net_pnl == Decimal(979)
    assert result.net_pnl_pct == pytest.approx(Decimal("9.79"))


def test_long_loss():
    result = tools_service.calculate_trade(
        side="buy",
        amount=Decimal(1),
        entry_price=Decimal(100),
        exit_price=Decimal(90),
        fee_pct=Decimal("0.1"),
    )

    assert result.gross_pnl == Decimal(-10)
    assert result.net_pnl < result.gross_pnl, "комиссия усугубляет убыток"


def test_short_profits_when_price_falls():
    result = tools_service.calculate_trade(
        side="sell",
        amount=Decimal(1),
        entry_price=Decimal(100),
        exit_price=Decimal(90),
        fee_pct=Decimal(0),
    )

    assert result.gross_pnl == Decimal(10)
    assert result.net_pnl == Decimal(10)


def test_zero_fee_leaves_pnl_untouched():
    result = tools_service.calculate_trade(
        side="buy",
        amount=Decimal(2),
        entry_price=Decimal(50),
        exit_price=Decimal(60),
        fee_pct=Decimal(0),
    )

    assert result.total_fees == Decimal(0)
    assert result.net_pnl == result.gross_pnl == Decimal(20)


def test_breakeven_covers_both_fees():
    """At the break-even exit price the result really is zero."""
    entry = Decimal(100)
    fee = Decimal("0.5")
    result = tools_service.calculate_trade(
        side="buy", amount=Decimal(1), entry_price=entry, exit_price=entry, fee_pct=fee
    )
    assert result.net_pnl < 0, "продажа по цене входа даёт убыток на комиссиях"

    at_breakeven = tools_service.calculate_trade(
        side="buy",
        amount=Decimal(1),
        entry_price=entry,
        exit_price=result.breakeven_price,
        fee_pct=fee,
    )
    assert abs(at_breakeven.net_pnl) < Decimal("0.0000000001")


def test_short_breakeven_is_below_entry():
    result = tools_service.calculate_trade(
        side="sell",
        amount=Decimal(1),
        entry_price=Decimal(100),
        exit_price=Decimal(100),
        fee_pct=Decimal("0.5"),
    )
    assert result.breakeven_price < Decimal(100)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount": Decimal(0)},
        {"entry_price": Decimal(0)},
        {"exit_price": Decimal(-1)},
        {"fee_pct": Decimal(-1)},
        {"side": "вбок"},
    ],
)
def test_bad_trade_input_rejected(kwargs):
    params = {
        "side": "buy",
        "amount": Decimal(1),
        "entry_price": Decimal(100),
        "exit_price": Decimal(110),
        "fee_pct": Decimal("0.1"),
    }
    params.update(kwargs)

    with pytest.raises(tools_service.ToolsError):
        tools_service.calculate_trade(**params)


# --- Converter ---


@pytest_asyncio.fixture
async def prices(session):
    exchange = Exchange(code="bybit", name="Bybit", sort_order=10)
    session.add(exchange)
    await session.flush()

    adapter = fakes.FakeAdapter(
        markets=[
            fakes.market("BTC/USDT", "BTC", "USDT"),
            fakes.market("ETH/USDT", "ETH", "USDT"),
        ]
    )
    await market_service.sync_markets(session, exchange, adapter)

    btc = await market_service.get_market(session, exchange.id, "BTC/USDT")
    eth = await market_service.get_market(session, exchange.id, "ETH/USDT")
    session.add(MarketTicker(market_id=btc.id, last=Decimal(80_000)))
    session.add(MarketTicker(market_id=eth.id, last=Decimal(2_000)))
    await session.commit()


async def test_convert_between_coins(session, prices):
    result = await tools_service.convert(
        session, amount=Decimal(1), source="BTC", target="ETH"
    )

    assert result.result == Decimal(40), "80 000 / 2 000"


async def test_convert_to_stablecoin(session, prices):
    result = await tools_service.convert(
        session, amount=Decimal("0.5"), source="BTC", target="USDT"
    )

    assert result.result == Decimal(40_000)


async def test_convert_is_case_insensitive(session, prices):
    result = await tools_service.convert(
        session, amount=Decimal(1), source=" btc ", target="usdt"
    )
    assert result.source == "BTC"
    assert result.target == "USDT"


async def test_convert_reports_missing_price(session, prices):
    with pytest.raises(tools_service.ToolsError) as exc:
        await tools_service.convert(
            session, amount=Decimal(1), source="NOSUCH", target="USDT"
        )

    assert "NOSUCH" in str(exc.value)


async def test_convert_rejects_non_positive_amount(session, prices):
    with pytest.raises(tools_service.ToolsError):
        await tools_service.convert(session, amount=Decimal(0), source="BTC", target="USDT")
