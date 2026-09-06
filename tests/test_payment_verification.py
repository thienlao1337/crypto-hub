"""Подтверждение поступления денег.

Проверяется главное свойство заглушки: она не имеет права ответить
«подтверждено» ни при каких обстоятельствах. Ошибка здесь означает выдачу
криптовалюты по неоплаченному заказу.
"""

from decimal import Decimal

from app.services import payment_verification


async def test_stub_never_confirms():
    verifier = payment_verification.NotConfiguredVerifier()

    check = await verifier.verify(amount=Decimal(9700), currency="RUB", reference="ord-1")

    assert check.is_confirmed is False


async def test_stub_says_it_could_not_check_rather_than_no():
    """«Проверять нечем» и «денег нет» — разные новости.

    На первое разбираются с настройками, на второе — с покупателем.
    """
    check = await payment_verification.NotConfiguredVerifier().verify(
        amount=Decimal(1), currency="RUB", reference="x"
    )

    assert check.is_unknown is True
    assert "не настроена" in check.reason


def test_verifier_is_not_configured_by_default():
    """Пока банк или шлюз не названы, автоотпуск невозможен по построению."""
    assert payment_verification.is_configured() is False
