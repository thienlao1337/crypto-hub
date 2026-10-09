"""Bybit P2P.

Endpoints /v5/p2p/*, signed the usual v5 way: HMAC-SHA256 over timestamp + api_key +
recv_window + request body.

Access is only open to accounts with General Advertiser status or higher. A regular key
gets rejected, and that rejection is fixed by applying on the marketplace, not by
retrying - hence a separate error type.

There is no way to test live without advertiser status, so response parsing follows the
documentation and fails with the name of the missing field instead of substituting a
default.
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

# Rejection codes caused by missing advertiser status.
ACCESS_CODES = {10005, 912100013}


class BybitP2PAdapter:
    """A single connection to Bybit P2P."""

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

    # --- Signing ---

    def sign(self, timestamp: str, body: str) -> str:
        """v5 request signature.

        The order of parts is defined by the exchange and matters: a reordering produces
        a formally valid signature that the server rejects.
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
        """Unwrap the v5 envelope and turn rejections into our errors."""
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
            # The marketplace's text is not shown in the UI - it may contain
            # the request URL and service fields; we expose a code and an
            # explanation.
            logger.warning("Bybit P2P %s: %s %s", path, code, data.get("retMsg"))
            raise P2PError(f"Bybit отклонил запрос (код {code}).")

        return data.get("result") or {}

    # --- Access ---

    async def check_access(self) -> P2PAccess:
        try:
            await self._post("/v5/p2p/item/personal/list", {"page": 1, "size": 1})
        except P2PAccessDenied as exc:
            return P2PAccess(is_allowed=False, error=str(exc))
        except P2PError as exc:
            return P2PAccess(is_allowed=False, error=str(exc))
        return P2PAccess(is_allowed=True)

    # --- Ads ---

    async def fetch_my_ads(self) -> list[AdInfo]:
        result = await self._post("/v5/p2p/item/personal/list", {"page": 1, "size": 100})
        return [self._parse_ad(item) for item in result.get("items") or []]

    async def fetch_board(
        self, *, side: str, asset: str, fiat: str, payment: str | None = None
    ) -> list[BoardEntry]:
        """Other ads on the same side.

        Bybit labels sides from the counterparty's point of view: to see our selling
        neighbours we have to ask for the buy side.
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

    # --- Orders ---

    async def fetch_orders(self) -> list[OrderInfo]:
        result = await self._post("/v5/p2p/order/simplifyList", {"page": 1, "size": 50})
        return [self._parse_order(item) for item in result.get("items") or []]

    async def release_order(self, external_id: str) -> None:
        await self._post("/v5/p2p/order/finish", {"orderId": external_id})

    # --- Response parsing ---

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
