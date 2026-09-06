"""P2P Bybit.

Эндпоинты /v5/p2p/*, подпись — обычная для v5: HMAC-SHA256 от строки
timestamp + api_key + recv_window + тело запроса.

Доступ открыт только аккаунтам со статусом General Advertiser и выше.
Обычный ключ получает отказ, и отказ этот чинится заявкой на площадке, а
не повтором запроса — поэтому он отдельным типом ошибки.

Проверить вживую без статуса рекламодателя нечем, поэтому разбор ответов
написан по документации и падает с именем недостающего поля вместо того,
чтобы подставлять значение по умолчанию.
"""

import hashlib
import hmac
import logging
import time
from decimal import Decimal

import httpx

from app.exchanges.p2p.base import (
    AdInfo,
    BoardEntry,
    OrderInfo,
    P2PAccess,
    P2PAccessDenied,
    P2PError,
    optional_decimal,
    require_decimal,
    to_decimal,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bybit.com"
TESTNET_URL = "https://api-testnet.bybit.com"
RECV_WINDOW = "5000"
TIMEOUT = 15

# Коды отказа из-за отсутствия статуса рекламодателя.
ACCESS_CODES = {10005, 912100013}


class BybitP2PAdapter:
    """Одно подключение к P2P Bybit."""

    code = "bybit"

    def __init__(self, api_key: str, api_secret: str, *, testnet: bool = False) -> None:
        self._key = api_key
        self._secret = api_secret
        self._client = httpx.AsyncClient(
            base_url=TESTNET_URL if testnet else BASE_URL, timeout=TIMEOUT
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BybitP2PAdapter":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    # --- Подпись ---

    def sign(self, timestamp: str, body: str) -> str:
        """Подпись запроса v5.

        Порядок частей задан биржей и важен: перестановка даёт формально
        правильную подпись, которую отвергнет сервер.
        """
        payload = f"{timestamp}{self._key}{RECV_WINDOW}{body}"
        return hmac.new(
            self._secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    def _headers(self, body: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        return {
            "X-BAPI-API-KEY": self._key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": self.sign(timestamp, body),
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: dict) -> dict:
        import json

        body = json.dumps(payload, separators=(",", ":"))
        try:
            response = await self._client.post(path, content=body, headers=self._headers(body))
        except httpx.HTTPError as exc:
            raise P2PError("Не удалось связаться с Bybit.") from exc

        return self._unwrap(response, path)

    def _unwrap(self, response: httpx.Response, path: str) -> dict:
        """Развернуть конверт v5 и перевести отказы в наши ошибки."""
        try:
            data = response.json()
        except ValueError as exc:
            raise P2PError(f"Bybit ответил не JSON на {path}.") from exc

        code = data.get("retCode")
        if code in ACCESS_CODES:
            raise P2PAccessDenied(
                "Ключу закрыт доступ к P2P. Нужен статус рекламодателя "
                "(General Advertiser и выше) в кабинете Bybit."
            )
        if code not in (0, None):
            # Текст площадки в интерфейс не выносим — там бывает и адрес
            # запроса, и служебные поля; наружу идёт код и объяснение.
            logger.warning("Bybit P2P %s: %s %s", path, code, data.get("retMsg"))
            raise P2PError(f"Bybit отклонил запрос (код {code}).")

        return data.get("result") or {}

    # --- Доступ ---

    async def check_access(self) -> P2PAccess:
        try:
            await self._post("/v5/p2p/item/personal/list", {"page": 1, "size": 1})
        except P2PAccessDenied as exc:
            return P2PAccess(is_allowed=False, error=str(exc))
        except P2PError as exc:
            return P2PAccess(is_allowed=False, error=str(exc))
        return P2PAccess(is_allowed=True)

    # --- Объявления ---

    async def fetch_my_ads(self) -> list[AdInfo]:
        result = await self._post("/v5/p2p/item/personal/list", {"page": 1, "size": 100})
        return [self._parse_ad(item) for item in result.get("items") or []]

    async def fetch_board(
        self, *, side: str, asset: str, fiat: str, payment: str | None = None
    ) -> list[BoardEntry]:
        """Чужие объявления той же стороны.

        Bybit нумерует стороны с точки зрения контрагента: чтобы увидеть
        соседей по своей продаже, спрашивать надо сторону покупки.
        """
        payload = {
            "tokenId": asset,
            "currencyId": fiat,
            "side": "0" if side == "sell" else "1",
            "page": 1,
            "size": 30,
        }
        if payment:
            payload["payment"] = [payment]

        result = await self._post("/v5/p2p/item/online", payload)
        return [self._parse_board_entry(item) for item in result.get("items") or []]

    async def update_ad_price(self, external_id: str, price: Decimal) -> None:
        await self._post(
            "/v5/p2p/item/update", {"id": external_id, "price": format(price, "f")}
        )

    # --- Заказы ---

    async def fetch_orders(self) -> list[OrderInfo]:
        result = await self._post("/v5/p2p/order/simplifyList", {"page": 1, "size": 50})
        return [self._parse_order(item) for item in result.get("items") or []]

    async def release_order(self, external_id: str) -> None:
        await self._post("/v5/p2p/order/finish", {"orderId": external_id})

    # --- Разбор ответов ---

    def _parse_ad(self, item: dict) -> AdInfo:
        return AdInfo(
            external_id=str(item.get("id") or ""),
            side="sell" if str(item.get("side")) == "1" else "buy",
            asset=str(item.get("tokenId") or ""),
            fiat=str(item.get("currencyId") or ""),
            price=require_decimal(item, "price", context="объявление Bybit"),
            quantity=optional_decimal(item, "quantity", "lastQuantity"),
            min_amount=optional_decimal(item, "minAmount"),
            max_amount=optional_decimal(item, "maxAmount"),
            status="online" if str(item.get("status")) == "10" else "offline",
            payment_methods=[str(p) for p in (item.get("payments") or [])],
            raw=item,
        )

    def _parse_board_entry(self, item: dict) -> BoardEntry:
        return BoardEntry(
            price=require_decimal(item, "price", context="доска Bybit"),
            available=optional_decimal(item, "lastQuantity", "quantity"),
            min_amount=optional_decimal(item, "minAmount"),
            max_amount=optional_decimal(item, "maxAmount"),
            merchant=str(item.get("nickName") or "") or None,
            completion_rate=optional_decimal(item, "recentExecuteRate"),
            orders_count=_as_int(item.get("recentOrderNum")),
            external_id=str(item.get("id") or "") or None,
        )

    def _parse_order(self, item: dict) -> OrderInfo:
        return OrderInfo(
            external_id=str(item.get("id") or ""),
            side="sell" if str(item.get("side")) == "1" else "buy",
            asset=str(item.get("tokenId") or ""),
            fiat=str(item.get("currencyId") or ""),
            status=str(item.get("status") or ""),
            amount=to_decimal(item.get("quantity")),
            fiat_amount=to_decimal(item.get("amount")),
            price=to_decimal(item.get("price")),
            counterparty=str(item.get("targetNickName") or "") or None,
            raw=item,
        )


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
