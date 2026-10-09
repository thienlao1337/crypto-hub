"""P2P marketplace adapters.

There's no way to test them against the live API: access is only open to accounts with
advertiser or merchant status. So we test what doesn't depend on it: request signing,
turning rejections into clear errors and response parsing - including the case where the
response format has changed.

The last one matters more than usual here. An ad price is money, and a zero silently
substituted for a changed field is worse than a loud failure.
"""

import hashlib
import hmac
from decimal import Decimal

import httpx
import pytest

from app.exchanges.p2p import build_adapter
from app.exchanges.p2p.base import P2PAccessDenied, P2PError, P2PResponseError
from app.exchanges.p2p.binance import BinanceP2PAdapter
from app.exchanges.p2p.bybit import BybitP2PAdapter

# Keys look like the ones exchanges issue: Latin letters and digits. HTTP
# headers accept nothing else anyway.
KEY = "bybitKey1234567890"
SECRET = "bybitSecret0987654321"


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --- Signing ---


def test_bybit_signature_matches_documented_scheme():
    """The order of parts is defined by the exchange: reordering gets rejected by the server."""
    adapter = BybitP2PAdapter(KEY, SECRET)

    signature = adapter.sign("1700000000000", '{"page":1}')

    expected = hmac.new(
        SECRET.encode(),
        f'1700000000000{KEY}5000{{"page":1}}'.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected


def test_binance_signature_is_taken_from_the_exact_query():
    """The signature is computed over exactly the string that will be sent to the server."""
    adapter = BinanceP2PAdapter(KEY, SECRET)

    query = "page=1&rows=10&timestamp=1700000000000"
    expected = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

    assert adapter.sign(query) == expected


def test_binance_signed_query_carries_signature_and_timestamp():
    adapter = BinanceP2PAdapter(KEY, SECRET)

    query = adapter._signed_query({"page": 1})

    assert "timestamp=" in query and "signature=" in query
    body, _, signature = query.rpartition("&signature=")
    assert adapter.sign(body) == signature


def test_secret_never_travels_in_the_request():
    """The signature is all that goes outside; the secret itself stays with us."""
    adapter = BinanceP2PAdapter(KEY, SECRET)

    assert SECRET not in adapter._signed_query({"page": 1})


# --- Rejection due to missing status ---


async def test_bybit_reports_missing_advertiser_status():
    """Such a rejection is fixed by applying on the marketplace, not by retrying."""

    def handler(request):
        return httpx.Response(200, json={"retCode": 10005, "retMsg": "permission denied"})

    adapter = BybitP2PAdapter(KEY, SECRET)
    adapter._client = httpx.AsyncClient(transport=transport(handler), base_url="https://x")

    async with adapter:
        access = await adapter.check_access()

    assert access.is_allowed is False
    assert "рекламодателя" in access.error


async def test_binance_reports_missing_merchant_status():
    def handler(request):
        return httpx.Response(401, json={"code": -2015, "msg": "invalid api key"})

    adapter = BinanceP2PAdapter(KEY, SECRET)
    adapter._client = httpx.AsyncClient(transport=transport(handler), base_url="https://x")

    async with adapter:
        access = await adapter.check_access()

    assert access.is_allowed is False
    assert "мерчанта" in access.error


async def test_bybit_access_check_passes_on_success():
    def handler(request):
        return httpx.Response(200, json={"retCode": 0, "result": {"items": []}})

    adapter = BybitP2PAdapter(KEY, SECRET)
    adapter._client = httpx.AsyncClient(transport=transport(handler), base_url="https://x")

    async with adapter:
        assert (await adapter.check_access()).is_allowed is True


async def test_platform_error_text_does_not_leak_outside():
    """The marketplace's text may contain the request URL and service fields."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "retCode": 33004,
                "retMsg": "GET https://api.bybit.com/v5/p2p?api_key=AAA&sign=deadbeef",
            },
        )

    adapter = BybitP2PAdapter(KEY, SECRET)
    adapter._client = httpx.AsyncClient(transport=transport(handler), base_url="https://x")

    async with adapter:
        with pytest.raises(P2PError) as info:
            await adapter.fetch_my_ads()

    assert "sign=" not in str(info.value)
    assert "33004" in str(info.value)


# --- Response parsing ---


async def test_bybit_board_is_parsed():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "items": [
                        {
                            "id": "42",
                            "price": "101.5",
                            "lastQuantity": "500",
                            "minAmount": "1000",
                            "maxAmount": "50000",
                            "nickName": "сосед",
                            "recentExecuteRate": "98",
                            "recentOrderNum": "120",
                        }
                    ]
                },
            },
        )

    adapter = BybitP2PAdapter(KEY, SECRET)
    adapter._client = httpx.AsyncClient(transport=transport(handler), base_url="https://x")

    async with adapter:
        board = await adapter.fetch_board(side="sell", asset="USDT", fiat="RUB")

    assert len(board) == 1
    assert board[0].price == Decimal("101.5")
    assert board[0].external_id == "42"
    assert board[0].completion_rate == Decimal(98)
    assert board[0].max_amount == Decimal(50000)


async def test_missing_price_field_fails_loudly():
    """The response format changed - a failure is better than a price out of nowhere."""

    def handler(request):
        return httpx.Response(
            200, json={"retCode": 0, "result": {"items": [{"id": "42", "quantity": "500"}]}}
        )

    adapter = BybitP2PAdapter(KEY, SECRET)
    adapter._client = httpx.AsyncClient(transport=transport(handler), base_url="https://x")

    async with adapter:
        with pytest.raises(P2PResponseError) as info:
            await adapter.fetch_board(side="sell", asset="USDT", fiat="RUB")

    assert "price" in str(info.value)


async def test_binance_board_normalises_completion_rate():
    """The storefront reports the share as 0.98, while the rule compares percentages."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "adv": {
                            "advNo": "77",
                            "price": "99.4",
                            "surplusAmount": "300",
                            "minSingleTransAmount": "500",
                            "maxSingleTransAmount": "30000",
                        },
                        "advertiser": {
                            "nickName": "сосед",
                            "monthFinishRate": "0.98",
                            "monthOrderCount": "80",
                        },
                    }
                ]
            },
        )

    adapter = BinanceP2PAdapter(KEY, SECRET)
    adapter._public = httpx.AsyncClient(transport=transport(handler), base_url="https://x")

    async with adapter:
        board = await adapter.fetch_board(side="sell", asset="USDT", fiat="RUB")

    assert board[0].price == Decimal("99.4")
    assert board[0].completion_rate == Decimal(98), "иначе фильтр надёжности отбросит всех"


async def test_binance_ad_is_parsed():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "code": "000000",
                "data": [
                    {
                        "advNo": "77",
                        "tradeType": "SELL",
                        "asset": "USDT",
                        "fiatUnit": "RUB",
                        "price": "101.2",
                        "surplusAmount": "250",
                        "advStatus": "1",
                        "tradeMethods": [{"identifier": "TinkoffNew"}],
                    }
                ],
            },
        )

    adapter = BinanceP2PAdapter(KEY, SECRET)
    adapter._client = httpx.AsyncClient(transport=transport(handler), base_url="https://x")

    async with adapter:
        ads = await adapter.fetch_my_ads()

    assert ads[0].external_id == "77"
    assert ads[0].side == "sell"
    assert ads[0].price == Decimal("101.2")
    assert ads[0].status == "online"
    assert ads[0].payment_methods == ["TinkoffNew"]


# --- Price update ---


async def test_bybit_update_sends_price_without_exponent():
    """Decimal can produce 1E+2 - the marketplace won't understand such a price."""
    sent = {}

    def handler(request):
        import json

        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"retCode": 0, "result": {}})

    adapter = BybitP2PAdapter(KEY, SECRET)
    adapter._client = httpx.AsyncClient(transport=transport(handler), base_url="https://x")

    async with adapter:
        await adapter.update_ad_price("42", Decimal("1E+2"))

    assert sent["price"] == "100"


async def test_unsupported_exchange_is_refused():
    with pytest.raises(P2PError):
        build_adapter("kraken", KEY, SECRET)


def test_factory_builds_both_platforms():
    assert isinstance(build_adapter("bybit", KEY, SECRET), BybitP2PAdapter)
    assert isinstance(build_adapter("binance", KEY, SECRET), BinanceP2PAdapter)
