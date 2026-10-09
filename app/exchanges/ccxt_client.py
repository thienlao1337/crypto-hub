"""Exchange adapter on top of ccxt.

One implementation for Bybit and Binance: ccxt unifies symbols, errors and the data
model, and the differences are reduced to a few places below.

An instance holds an open http session, so it must be closed. Use it as an async context
manager.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

import ccxt.async_support as ccxt

from app.exchanges.base import (
    BalanceEntry,
    ExchangeAuthError,
    ExchangeError,
    ExchangeRateLimited,
    ExchangeUnavailable,
    KeyCheck,
    MarketInfo,
    OrderResult,
    OhlcvBar,
    TickerInfo,
    TradeInfo,
    to_decimal,
)

logger = logging.getLogger(__name__)

EXCHANGE_BYBIT = "bybit"
EXCHANGE_BINANCE = "binance"
SUPPORTED = (EXCHANGE_BYBIT, EXCHANGE_BINANCE)


class CcxtAdapter:
    """Wrapper around a single exchange connection."""

    def __init__(
        self,
        exchange_code: str,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        testnet: bool = False,
    ) -> None:
        if exchange_code not in SUPPORTED:
            raise ExchangeError(f"Биржа {exchange_code} не поддерживается.")

        self.code = exchange_code
        self.testnet = testnet

        # Load only spot markets for both exchanges. Otherwise ccxt also pulls
        # options (Bybit) and margin pairs (Binance): extra requests, extra
        # points of failure, and Binance's margin endpoint also requires
        # permissions a read-only key may not have - so checking a regular key
        # failed with an obscure error.
        options: dict = {"defaultType": "spot", "fetchMarkets": ["spot"]}

        factory = getattr(ccxt, exchange_code)
        self._client = factory(
            {
                "apiKey": api_key or "",
                "secret": api_secret or "",
                # ccxt spaces out requests itself - without that the public
                # limits are used up in seconds.
                "enableRateLimit": True,
                "options": options,
            }
        )
        if testnet:
            self._client.set_sandbox_mode(True)

    async def __aenter__(self) -> "CcxtAdapter":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.close()

    # --- Key check ---

    async def check_key(self) -> KeyCheck:
        """Make sure the key works and find out whether it can trade.

        Trading permission is confirmed only by an explicit exchange response. If it
        can't be determined, we assume there is no permission: access to live trading
        must never be granted on a guess.
        """
        try:
            await self._client.fetch_balance()
        except ccxt.AuthenticationError:
            return KeyCheck(is_valid=False, error="Биржа отклонила ключ: неверный ключ или секрет.")
        except ccxt.PermissionDenied:
            return KeyCheck(
                is_valid=False,
                error="У ключа недостаточно прав. Проверьте разрешения в кабинете биржи.",
            )
        except ccxt.RateLimitExceeded:
            return KeyCheck(
                is_valid=False,
                error="Биржа временно ограничила запросы. Попробуйте через минуту.",
            )
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable):
            return KeyCheck(
                is_valid=False, error="Не удалось связаться с биржей. Попробуйте позже."
            )
        except ccxt.BaseError as exc:
            # The ccxt message here is usually the raw request URL together
            # with the signature. That must not reach the UI: it means nothing
            # to the user, and the signature has no place in the markup.
            logger.warning("Key check for %s failed: %s", self.code, exc)
            return KeyCheck(
                is_valid=False,
                error=(
                    f"Биржа не приняла ключ ({exc.__class__.__name__}). "
                    "Подробности — в журнале сервера."
                ),
            )

        can_trade, known = await self._probe_trading_permission()
        return KeyCheck(is_valid=True, can_trade=can_trade, permissions_known=known)

    async def _probe_trading_permission(self) -> tuple[bool, bool]:
        """Ask the exchange whether the key is allowed to trade.

        Endpoints differ between exchanges and change over time, so the call is guarded:
        on anything unexpected we return "unknown", and the caller treats that as no
        permission.
        """
        try:
            if self.code == EXCHANGE_BYBIT:
                method = getattr(self._client, "privateGetV5UserQueryApi", None)
                if method is None:
                    return False, False
                response = await method()
                permissions = (response.get("result") or {}).get("permissions") or {}
                spot = permissions.get("Spot") or []
                return bool(spot), True

            if self.code == EXCHANGE_BINANCE:
                method = getattr(self._client, "sapiGetAccountApiRestrictions", None)
                if method is None:
                    return False, False
                response = await method()
                return bool(response.get("enableSpotAndMarginTrading")), True
        except ccxt.BaseError as exc:
            logger.info("Could not determine permissions of key %s: %s", self.code, exc)
            return False, False
        except Exception:
            logger.exception("Failure while checking permissions of key %s", self.code)
            return False, False

        return False, False

    # --- Account data ---

    async def fetch_balances(self) -> list[BalanceEntry]:
        raw = await self._call(self._client.fetch_balance)

        entries: list[BalanceEntry] = []
        totals = raw.get("total") or {}
        free = raw.get("free") or {}
        used = raw.get("used") or {}

        for asset, total in totals.items():
            total_dec = to_decimal(total) or Decimal(0)
            if total_dec <= 0:
                # Exchanges return zero balances by the hundred - they only
                # clutter the portfolio.
                continue
            entries.append(
                BalanceEntry(
                    asset=asset,
                    free=to_decimal(free.get(asset)) or Decimal(0),
                    locked=to_decimal(used.get(asset)) or Decimal(0),
                    total=total_dec,
                )
            )
        return entries

    async def fetch_my_trades(
        self,
        symbol: str,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[TradeInfo]:
        raw = await self._call(
            self._client.fetch_my_trades,
            symbol,
            _to_millis(since),
            limit,
        )

        trades: list[TradeInfo] = []
        for item in raw:
            fee = item.get("fee") or {}
            trades.append(
                TradeInfo(
                    external_id=str(item.get("id")),
                    order_id=str(item["order"]) if item.get("order") else None,
                    symbol=item.get("symbol") or symbol,
                    side=item.get("side") or "",
                    price=to_decimal(item.get("price")) or Decimal(0),
                    amount=to_decimal(item.get("amount")) or Decimal(0),
                    cost=to_decimal(item.get("cost")) or Decimal(0),
                    fee=to_decimal(fee.get("cost")),
                    fee_asset=fee.get("currency"),
                    executed_at=_from_millis(item.get("timestamp")),
                    raw=item.get("info") or {},
                )
            )
        return trades

    async def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
    ) -> OrderResult:
        """Place a market order.

        Market orders only: a limit order needs order lifecycle management, while the
        strategies in the spec enter and exit at market.
        """
        if side not in ("buy", "sell"):
            raise ExchangeError(f"Неизвестное направление ордера: {side}")

        # The amount comes from a deposit-share calculation and looks like
        # 0.05358804425365755979124688685 - the exchange would reject such an
        # order outright: it only accepts multiples of its lot step. ccxt knows
        # the step, but that requires the loaded instrument list; load_markets
        # caches it, so the call is cheap.
        await self._call(self._client.load_markets)
        rounded = to_decimal(self._client.amount_to_precision(symbol, float(amount)))

        if rounded is None or rounded <= 0:
            raise ExchangeError(
                f"Объём {amount} меньше шага лота {symbol} — ордер не выставлен."
            )

        raw = await self._call(
            self._client.create_order, symbol, "market", side, float(rounded)
        )

        return OrderResult(
            external_id=str(raw.get("id") or ""),
            symbol=raw.get("symbol") or symbol,
            side=raw.get("side") or side,
            amount=to_decimal(raw.get("amount")) or rounded,
            price=to_decimal(raw.get("price")),
            status=raw.get("status") or "unknown",
            filled=to_decimal(raw.get("filled")) or Decimal(0),
            average_price=to_decimal(raw.get("average")),
            raw=raw.get("info") or {},
        )

    # --- Market data (no keys needed) ---

    async def fetch_markets(self) -> list[MarketInfo]:
        raw = await self._call(self._client.load_markets)

        markets: list[MarketInfo] = []
        for symbol, market in raw.items():
            if not market.get("spot") or not market.get("active"):
                continue

            limits = (market.get("limits") or {}).get("amount") or {}
            precision = market.get("precision") or {}
            markets.append(
                MarketInfo(
                    symbol=symbol,
                    raw_symbol=market.get("id") or symbol.replace("/", ""),
                    base=market.get("base") or "",
                    quote=market.get("quote") or "",
                    price_precision=_as_int(precision.get("price")),
                    amount_precision=_as_int(precision.get("amount")),
                    min_amount=to_decimal(limits.get("min")),
                    tick_size=to_decimal(precision.get("price")),
                )
            )
        return markets

    async def fetch_tickers(self, symbols: list[str] | None = None) -> list[TickerInfo]:
        raw = await self._call(self._client.fetch_tickers, symbols)

        tickers: list[TickerInfo] = []
        for symbol, ticker in raw.items():
            tickers.append(
                TickerInfo(
                    symbol=symbol,
                    last=to_decimal(ticker.get("last")),
                    bid=to_decimal(ticker.get("bid")),
                    ask=to_decimal(ticker.get("ask")),
                    high_24h=to_decimal(ticker.get("high")),
                    low_24h=to_decimal(ticker.get("low")),
                    volume_24h=to_decimal(ticker.get("baseVolume")),
                    quote_volume_24h=to_decimal(ticker.get("quoteVolume")),
                    change_24h_pct=to_decimal(ticker.get("percentage")),
                )
            )
        return tickers

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[OhlcvBar]:
        raw = await self._call(
            self._client.fetch_ohlcv,
            symbol,
            timeframe,
            _to_millis(since),
            limit,
        )

        bars: list[OhlcvBar] = []
        for row in raw:
            bars.append(
                OhlcvBar(
                    open_time=_from_millis(row[0]),
                    open=to_decimal(row[1]) or Decimal(0),
                    high=to_decimal(row[2]) or Decimal(0),
                    low=to_decimal(row[3]) or Decimal(0),
                    close=to_decimal(row[4]) or Decimal(0),
                    volume=to_decimal(row[5]) or Decimal(0),
                )
            )
        return bars

    # --- Common error handling ---

    async def _call(self, method, *args):
        """Map ccxt errors to our own types.

        The caller needs to tell three cases apart: bad key (fix by hand), rate limit
        (wait), network down (retry later).
        """
        try:
            return await method(*args)
        except ccxt.AuthenticationError as exc:
            raise ExchangeAuthError(self._explain(exc, "Биржа отклонила ключ.")) from exc
        except ccxt.PermissionDenied as exc:
            raise ExchangeAuthError(
                self._explain(exc, "У ключа недостаточно прав для этой операции.")
            ) from exc
        except ccxt.RateLimitExceeded as exc:
            raise ExchangeRateLimited(
                self._explain(exc, "Биржа ограничила частоту запросов.")
            ) from exc
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
            raise ExchangeUnavailable(
                self._explain(exc, "Биржа сейчас недоступна.")
            ) from exc
        except ccxt.BaseError as exc:
            raise ExchangeError(
                self._explain(exc, f"Биржа вернула ошибку ({exc.__class__.__name__}).")
            ) from exc

    def _explain(self, exc: Exception, message: str) -> str:
        """A human-readable message for the outside, details to the log.

        The ccxt message is usually the request URL with the signature and service JSON.
        It ends up in last_error and from there in the UI, where it is useless to the
        user and explains nothing.
        """
        logger.warning("%s: %s", self.code, exc)
        return message


def _to_millis(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _from_millis(value) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _as_int(value) -> int | None:
    """ccxt reports precision either as an integer or as a step like 0.001."""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None
    if number == number.to_integral_value() and number >= 0:
        return int(number)
    exponent = number.as_tuple().exponent
    return int(-exponent) if isinstance(exponent, int) else None
