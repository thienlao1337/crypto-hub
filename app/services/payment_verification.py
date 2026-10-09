"""Confirmation that money has arrived.

The "paid" mark on P2P is set by the buyer, and the marketplace doesn't verify it - it
merely passes one side's word to the other. The only party that can confirm an arrival
is the source of the money: a bank or payment gateway.

So this is only an interface, with no implementation. The provider depends on the bank
and the country: Tinkoff, monobank and YooKassa are three different integrations, and it
can't be chosen on the client's behalf.

Until a provider is configured, automatic release of funds is impossible - not because
"we haven't got to it yet", but because releasing on an unconfirmed claim means handing
money to anyone who pressed a button. The difference between "verification isn't
configured" and "verification said no" is fundamental here, and the types preserve it.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaymentCheck:
    """What the source of money said about the payment."""

    # The money arrived and matches the order.
    is_confirmed: bool
    # Nothing to verify with: the provider isn't configured or is unavailable.
    # That's not a "no", and the two states must not be confused - a "no" means
    # dealing with the buyer, "nothing to verify with" means dealing with the
    # settings.
    is_unknown: bool = False
    reason: str = ""
    matched_amount: Decimal | None = None
    matched_at: datetime | None = None


class PaymentVerifier(Protocol):
    """A source capable of confirming that money has arrived."""

    async def verify(
        self,
        *,
        amount: Decimal,
        currency: str,
        reference: str,
        since: datetime | None = None,
    ) -> PaymentCheck:
        """Find an incoming payment for the given amount."""


class NotConfiguredVerifier:
    """Placeholder until a bank or gateway is named.

    Always answers "nothing to verify with", and that's exactly why auto-release doesn't
    fire with it. Such a placeholder has no right to silently return "confirmed" under
    any circumstances.
    """

    async def verify(
        self,
        *,
        amount: Decimal,
        currency: str,
        reference: str,
        since: datetime | None = None,
    ) -> PaymentCheck:
        return PaymentCheck(
            is_confirmed=False,
            is_unknown=True,
            reason=(
                "Проверка поступления не настроена: не указан банк или платёжный "
                "шлюз. Отпустите средства вручную, убедившись в приходе."
            ),
        )


def get_verifier() -> PaymentVerifier:
    """The configured verification provider.

    A separate function so plugging in a provider is a one-line change, not edits all
    over the release code.
    """
    return NotConfiguredVerifier()


def is_configured() -> bool:
    return not isinstance(get_verifier(), NotConfiguredVerifier)
