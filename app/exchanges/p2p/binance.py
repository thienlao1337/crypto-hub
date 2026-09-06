"""P2P Binance.

Управление объявлениями — sapi/v1/c2c/agent/ads/*, подпись обычная для
sapi: HMAC-SHA256 от строки запроса с timestamp, ключ в заголовке
X-MBX-APIKEY. Доска чужих объявлений берётся публичным эндпоинтом, для
него ключ не нужен вовсе.

Доступ к записи открыт только верифицированным мерчантам. Как и у Bybit,
отказ из-за отсутствия статуса — отдельный тип ошибки: он чинится
заявкой, а не повтором.

Проверить вживую без статуса мерчанта нечем, поэтому разбор ответов
падает с именем недостающего поля, а не подставляет значение по
умолчанию.
"""

import hashlib
import hmac
import logging
import time
from decimal import Decimal
from urllib.parse import urlencode

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

BASE_URL = "https://api.binance.com"
# Доска объявлений живёт на витрине сайта, а не в торговом API, и ключа
# не требует: это то же, что видит любой посетитель раздела P2P.
PUBLIC_URL = "https://p2p.binance.com"
TIMEOUT = 15
RECV_WINDOW = 5000

# Коды отказа из-за отсутствия прав мерчанта.
ACCESS_CODES = {-2015, -1002}


class BinanceP2PAdapter:
    """Одно подключение к P2P Binance."""

    code = "binance"

    def __init__(self, api_key: str, api_secret: str, *, testnet: bool = False) -> None:
        # Тестовой сети у P2P нет ни у одной площадки: объявления и
        # заказы существуют только в бою.
        self._key = api_key
        self._secret = api_secret
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT)
        self._public = httpx.AsyncClient(base_url=PUBLIC_URL, timeout=TIMEOUT)

    async def close(self) -> None:
        await self._client.aclose()
        await self._public.aclose()

    async def __aenter__(self) -> "BinanceP2PAdapter":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    # --- Подпись ---

    def sign(self, query: str) -> str:
        """Подпись строки запроса. Считается ровно от того, что уйдёт."""
        return hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _signed_query(self, params: dict) -> str:
        payload = dict(params)
        payload["timestamp"] = int(time.time() * 1000)
        payload["recvWindow"] = RECV_WINDOW
        query = urlencode(payload)
        return f"{query}&signature={self.sign(query)}"

    async def _post(self, path: str, params: dict) -> dict:
        query = self._signed_query(params)
        try:
            response = await self._client.post(
                f"{path}?{query}", headers={"X-MBX-APIKEY": self._key}
            )
        except httpx.HTTPError as exc:
            raise P2PError("Не удалось связаться с Binance.") from exc

        return self._unwrap(response, path)

    def _unwrap(self, response: httpx.Response, path: str) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise P2PError(f"Binance ответил не JSON на {path}.") from exc

        code = data.get("code")
        if code in ACCESS_CODES or response.status_code == 401:
            raise P2PAccessDenied(
                "Ключу закрыт доступ к P2P. Нужен статус верифицированного "
                "мерчанта в кабинете Binance."
            )
        if response.status_code >= 400 or (code not in (None, "000000", 0)):
            logger.warning("Binance P2P %s: %s %s", path, code, data.get("msg"))
            raise P2PError(f"Binance отклонил запрос (код {code}).")

        return data.get("data") or {}

    # --- Доступ ---

    async def check_access(self) -> P2PAccess:
        try:
            await self._post("/sapi/v1/c2c/agent/ads/listWithPagination", {"page": 1, "rows": 1})
        except P2PAccessDenied as exc:
            return P2PAccess(is_allowed=False, error=str(exc))
        except P2PError as exc:
            return P2PAccess(is_allowed=False, error=str(exc))
        return P2PAccess(is_allowed=True)

    # --- Объявления ---

    async def fetch_my_ads(self) -> list[AdInfo]:
        data = await self._post(
            "/sapi/v1/c2c/agent/ads/listWithPagination", {"page": 1, "rows": 100}
        )
        rows = data if isinstance(data, list) else data.get("data") or []
        return [self._parse_ad(item) for item in rows]

    async def fetch_board(
        self, *, side: str, asset: str, fiat: str, payment: str | None = None
    ) -> list[BoardEntry]:
        """Чужие объявления той же стороны.

        Binance называет стороны с точки зрения посетителя доски: наши
        соседи по продаже перечислены как предложения BUY.
        """
        payload = {
            "asset": asset,
            "fiat": fiat,
            "tradeType": "BUY" if side == "sell" else "SELL",
            "page": 1,
            "rows": 20,
            "payTypes": [payment] if payment else [],
        }
        try:
            response = await self._public.post(
                "/bapi/c2c/v2/friendly/c2c/adv/search", json=payload
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise P2PError("Не удалось получить доску объявлений Binance.") from exc

        return [self._parse_board_entry(item) for item in data.get("data") or []]

    async def update_ad_price(self, external_id: str, price: Decimal) -> None:
        await self._post(
            "/sapi/v1/c2c/agent/ads/update",
            {"advNo": external_id, "price": format(price, "f")},
        )

    # --- Заказы ---

    async def fetch_orders(self) -> list[OrderInfo]:
        data = await self._post(
            "/sapi/v1/c2c/orderMatch/listUserOrderHistory", {"page": 1, "rows": 50}
        )
        rows = data if isinstance(data, list) else data.get("data") or []
        return [self._parse_order(item) for item in rows]

    async def release_order(self, external_id: str) -> None:
        await self._post("/sapi/v1/c2c/agent/order/release", {"orderNumber": external_id})

    # --- Разбор ответов ---

    def _parse_ad(self, item: dict) -> AdInfo:
        return AdInfo(
            external_id=str(item.get("advNo") or ""),
            side="sell" if str(item.get("tradeType")).upper() == "SELL" else "buy",
            asset=str(item.get("asset") or ""),
            fiat=str(item.get("fiatUnit") or item.get("fiat") or ""),
            price=require_decimal(item, "price", context="объявление Binance"),
            quantity=optional_decimal(item, "surplusAmount", "initAmount"),
            min_amount=optional_decimal(item, "minSingleTransAmount"),
            max_amount=optional_decimal(item, "maxSingleTransAmount"),
            status="online" if str(item.get("advStatus")) in ("1", "PUBLISHED") else "offline",
            payment_methods=[
                str(method.get("identifier") or method.get("payType") or "")
                for method in (item.get("tradeMethods") or [])
            ],
            raw=item,
        )

    def _parse_board_entry(self, item: dict) -> BoardEntry:
        # Публичная витрина кладёт объявление в adv, а продавца в
        # advertiser: цена и надёжность лежат в разных половинах ответа.
        adv = item.get("adv") or {}
        advertiser = item.get("advertiser") or {}
        rate = optional_decimal(advertiser, "monthFinishRate")
        return BoardEntry(
            price=require_decimal(adv, "price", context="доска Binance"),
            available=optional_decimal(adv, "surplusAmount"),
            min_amount=optional_decimal(adv, "minSingleTransAmount"),
            max_amount=optional_decimal(adv, "maxSingleTransAmount"),
            merchant=str(advertiser.get("nickName") or "") or None,
            # Витрина отдаёт долю сделок как 0.98, а правило сравнивает с
            # процентами: приводим к одной шкале, иначе фильтр надёжности
            # отбросит вообще всех.
            completion_rate=rate * Decimal(100) if rate is not None else None,
            orders_count=_as_int(advertiser.get("monthOrderCount")),
            external_id=str(adv.get("advNo") or "") or None,
        )

    def _parse_order(self, item: dict) -> OrderInfo:
        return OrderInfo(
            external_id=str(item.get("orderNumber") or ""),
            side="sell" if str(item.get("tradeType")).upper() == "SELL" else "buy",
            asset=str(item.get("asset") or ""),
            fiat=str(item.get("fiat") or ""),
            status=str(item.get("orderStatus") or ""),
            amount=to_decimal(item.get("amount")),
            fiat_amount=to_decimal(item.get("totalPrice")),
            price=to_decimal(item.get("unitPrice")),
            counterparty=str(item.get("counterPartNickName") or "") or None,
            raw=item,
        )


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
