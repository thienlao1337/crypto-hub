"""Подтверждение поступления денег.

Отметку «оплачено» на P2P ставит покупатель, и площадка её не проверяет —
она лишь передаёт слова одной стороны другой. Единственный, кто может
подтвердить приход, — источник денег: банк или платёжный шлюз.

Поэтому здесь только стык, без реализации. Провайдер зависит от банка и
страны: под Тинькофф, монобанк и YooKassa это три разные интеграции, и
выбрать за клиента её нельзя.

Пока провайдер не настроен, автоматический отпуск средств невозможен —
не потому, что «не успели», а потому, что отпускать по неподтверждённому
заявлению значит отдавать деньги любому, кто нажал кнопку. Разница между
«проверка не настроена» и «проверка сказала нет» здесь принципиальна, и
типы её сохраняют.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaymentCheck:
    """Что источник денег сказал о платеже."""

    # Деньги пришли и сходятся с заказом.
    is_confirmed: bool
    # Проверять было нечем: провайдер не настроен или недоступен. Это не
    # «нет», и путать эти два состояния нельзя — на «нет» надо разбираться
    # с покупателем, а на «нечем» с настройками.
    is_unknown: bool = False
    reason: str = ""
    matched_amount: Decimal | None = None
    matched_at: datetime | None = None


class PaymentVerifier(Protocol):
    """Источник, способный подтвердить приход денег."""

    async def verify(
        self,
        *,
        amount: Decimal,
        currency: str,
        reference: str,
        since: datetime | None = None,
    ) -> PaymentCheck:
        """Найти поступление на указанную сумму."""


class NotConfiguredVerifier:
    """Заглушка на время, пока банк или шлюз не названы.

    Всегда отвечает «проверить нечем», и именно поэтому автоотпуск с ней
    не срабатывает. Молча возвращать «подтверждено» такая заглушка не
    имеет права ни при каких обстоятельствах.
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
    """Настроенный провайдер проверки.

    Отдельной функцией, чтобы подключение провайдера было заменой одной
    строки, а не правкой по всему коду отпуска.
    """
    return NotConfiguredVerifier()


def is_configured() -> bool:
    return not isinstance(get_verifier(), NotConfiguredVerifier)
