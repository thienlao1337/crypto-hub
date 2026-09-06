"""Адаптеры P2P-площадок.

Проверить их против живого API нечем: доступ открыт только аккаунтам со
статусом рекламодателя или мерчанта. Поэтому проверяется то, что от него
не зависит: подпись запроса, перевод отказов в понятные ошибки и разбор
ответа — включая случай, когда формат ответа изменился.

Последнее здесь важнее обычного. Цена объявления — это деньги, и молча
подставленный ноль вместо изменившегося поля хуже громкого отказа.
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

# Ключи такие же, как выдают биржи: латиница и цифры. Заголовки HTTP
# другого и не принимают.
KEY = "bybitKey1234567890"
SECRET = "bybitSecret0987654321"


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --- Подпись ---


def test_bybit_signature_matches_documented_scheme():
    """Порядок частей задан биржей: перестановка даст отказ сервера."""
    adapter = BybitP2PAdapter(KEY, SECRET)

    signature = adapter.sign("1700000000000", '{"page":1}')

    expected = hmac.new(
        SECRET.encode(),
        f'1700000000000{KEY}5000{{"page":1}}'.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected


def test_binance_signature_is_taken_from_the_exact_query():
    """Подпись считается ровно от той строки, которая уйдёт на сервер."""
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
    """Подпись — это всё, что уходит наружу; сам секрет остаётся у нас."""
    adapter = BinanceP2PAdapter(KEY, SECRET)

    assert SECRET not in adapter._signed_query({"page": 1})


# --- Отказ из-за отсутствия статуса ---


async def test_bybit_reports_missing_advertiser_status():
    """Такой отказ чинится заявкой на площадке, а не повтором запроса."""

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
    """В тексте площадки бывает и адрес запроса, и служебные поля."""

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


# --- Разбор ответов ---


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
    """Изменился формат ответа — лучше отказ, чем цена из ниоткуда."""

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
    """Витрина отдаёт долю как 0.98, а правило сравнивает с процентами."""

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


# --- Обновление цены ---


async def test_bybit_update_sends_price_without_exponent():
    """Decimal умеет выдавать 1E+2 — площадка такую цену не поймёт."""
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
