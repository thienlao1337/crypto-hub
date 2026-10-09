"""Confirmation that money has arrived.

We test the placeholder's key property: it has no right to answer "confirmed" under any
circumstances. A bug here means releasing crypto on an unpaid order.
"""

from decimal import Decimal

from app.services import payment_verification


async def test_stub_never_confirms():
    verifier = payment_verification.NotConfiguredVerifier()

    check = await verifier.verify(amount=Decimal(9700), currency="RUB", reference="ord-1")

    assert check.is_confirmed is False


async def test_stub_says_it_could_not_check_rather_than_no():
    """"Nothing to verify with" and "no money" are different news.

    The first means dealing with settings, the second with the buyer.
    """
    check = await payment_verification.NotConfiguredVerifier().verify(
        amount=Decimal(1), currency="RUB", reference="x"
    )

    assert check.is_unknown is True
    assert "не настроена" in check.reason


def test_verifier_is_not_configured_by_default():
    """Until a bank or gateway is named, auto-release is impossible by design."""
    assert payment_verification.is_configured() is False
