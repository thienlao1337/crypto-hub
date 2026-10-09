"""Common interface to P2P marketplaces.

ccxt doesn't fit here: it is about an exchange order book, while P2P is an ad board with
its own endpoints, its own signing and its own data model. Hence a separate protocol and
one implementation per marketplace.

Response parsing is deliberately strict: if a response lacks the field the price comes
from, the adapter raises an error naming that field instead of substituting zero or
skipping the record. A silently wrong price on the ad board is the client's money, and a
loud failure on the very first run is better than a plausible number of unknown origin.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol


class P2PError(Exception):
    """The marketplace didn't respond or refused."""


class P2PAccessDenied(P2PError):
    """No advertiser or merchant status.

    A separate type because it isn't fixed by retrying the request but by applying on
    the marketplace: the message should send the person there, not to the logs.
    """


class P2PResponseError(P2PError):
    """The response couldn't be fully parsed - a required field is missing."""


@dataclass(frozen=True)
class P2PAccess:
    """What the marketplace confirmed about our key."""

    is_allowed: bool
    error: str | None = None


@dataclass(frozen=True)
class AdInfo:
    """Our ad on the marketplace."""

    external_id: str
    side: str
    asset: str
    fiat: str
    price: Decimal
    quantity: Decimal | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    status: str = "online"
    payment_methods: list[str] = field(default_factory=list)
    raw: dict | None = None


@dataclass(frozen=True)
class BoardEntry:
    """Someone else's ad on the board - the neighbour we price against.

    completion_rate and orders_count aren't for reporting: they filter out ads not worth
    chasing. A fresh account with a single trade can set any price, drag us along and
    serve nobody in the process.
    """

    price: Decimal
    available: Decimal | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    merchant: str | None = None
    completion_rate: Decimal | None = None
    orders_count: int | None = None
    # We recognize our own ad on the board by its id: outbidding yourself is a
    # sure way to sink to the floor within a few passes.
    external_id: str | None = None


@dataclass(frozen=True)
class OrderInfo:
    """An order on our ad."""

    external_id: str
    side: str
    asset: str
    fiat: str
    status: str
    amount: Decimal | None = None
    fiat_amount: Decimal | None = None
    price: Decimal | None = None
    counterparty: str | None = None
    paid_at: datetime | None = None
    raw: dict | None = None


class P2PAdapter(Protocol):
    """What a marketplace must support for the bot to work with it."""

    async def check_access(self) -> P2PAccess:
        """Confirm that P2P endpoints are open to the key."""

    async def fetch_my_ads(self) -> list[AdInfo]:
        ...

    async def fetch_board(
        self, *, side: str, asset: str, fiat: str, payment: str | None = None
    ) -> list[BoardEntry]:
        """Other ads on the same side as ours."""

    async def update_ad_price(self, external_id: str, price: Decimal) -> None:
        ...

    async def fetch_orders(self) -> list[OrderInfo]:
        ...

    async def release_order(self, external_id: str) -> None:
        """Release crypto for an order."""

    async def close(self) -> None:
        ...


def require_decimal(payload: dict, *names: str, context: str) -> Decimal:
    """Get a number that must be present in the response.

    A missing field means the marketplace's response format changed or we're reading the
    wrong endpoint. Substituting a default here is not allowed: the bot moves the price
    based on these numbers.
    """
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            value = to_decimal(payload[name])
            if value is not None:
                return value

    raise P2PResponseError(
        f"{context}: в ответе площадки нет поля {' или '.join(names)}. "
        "Формат ответа изменился — цену по нему считать нельзя."
    )


def optional_decimal(payload: dict, *names: str) -> Decimal | None:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            value = to_decimal(payload[name])
            if value is not None:
                return value
    return None


def to_decimal(value) -> Decimal | None:
    """A number from the marketplace response.

    Via str, not float: 0.1 as a binary fraction isn't 0.1, and here it's a price.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
