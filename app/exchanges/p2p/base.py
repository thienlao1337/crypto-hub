"""Общий интерфейс к P2P-площадкам.

ccxt сюда не годится: он про биржевой стакан, а P2P — доска объявлений со
своими эндпоинтами, своей подписью и своей моделью данных. Поэтому свой
протокол и по реализации на площадку.

Разбор ответов намеренно нетерпимый: если в ответе нет поля, из которого
берётся цена, адаптер поднимает ошибку с именем этого поля, а не
подставляет ноль или пропускает запись. Молча неверная цена на доске
объявлений — это деньги клиента, и лучше громкий отказ на первом же
запуске, чем правдоподобная цифра неизвестного происхождения.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol


class P2PError(Exception):
    """Площадка не ответила или ответила отказом."""


class P2PAccessDenied(P2PError):
    """Нет статуса рекламодателя или мерчанта.

    Отдельным типом, потому что чинится не повтором запроса, а заявкой на
    площадке: сообщение должно вести человека туда, а не в логи.
    """


class P2PResponseError(P2PError):
    """Ответ разобран не полностью — в нём нет нужного поля."""


@dataclass(frozen=True)
class P2PAccess:
    """Что площадка подтвердила по нашему ключу."""

    is_allowed: bool
    error: str | None = None


@dataclass(frozen=True)
class AdInfo:
    """Наше объявление на площадке."""

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
    """Чужое объявление на доске — сосед, относительно которого считаем.

    completion_rate и orders_count нужны не для отчётности: по ним
    отсеиваются объявления, за которыми не стоит гнаться. Свежий аккаунт
    с одной сделкой может выставить любую цену, увести нас за собой и
    ничего при этом не обслужить.
    """

    price: Decimal
    available: Decimal | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    merchant: str | None = None
    completion_rate: Decimal | None = None
    orders_count: int | None = None
    # Своё объявление на доске узнаём по идентификатору: перебивать
    # самого себя — верный способ уехать в пол за несколько проходов.
    external_id: str | None = None


@dataclass(frozen=True)
class OrderInfo:
    """Заказ по нашему объявлению."""

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
    """Что должна уметь площадка, чтобы бот с ней работал."""

    async def check_access(self) -> P2PAccess:
        """Подтвердить, что ключу открыты P2P-эндпоинты."""

    async def fetch_my_ads(self) -> list[AdInfo]:
        ...

    async def fetch_board(
        self, *, side: str, asset: str, fiat: str, payment: str | None = None
    ) -> list[BoardEntry]:
        """Чужие объявления той же стороны, что и наше."""

    async def update_ad_price(self, external_id: str, price: Decimal) -> None:
        ...

    async def fetch_orders(self) -> list[OrderInfo]:
        ...

    async def release_order(self, external_id: str) -> None:
        """Отпустить криптовалюту по заказу."""

    async def close(self) -> None:
        ...


def require_decimal(payload: dict, *names: str, context: str) -> Decimal:
    """Достать число, которое обязано быть в ответе.

    Отсутствие такого поля означает, что формат ответа площадки изменился
    или мы читаем не тот эндпоинт. Подставлять здесь значение по
    умолчанию нельзя: на этих числах бот двигает цену.
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
    """Число из ответа площадки.

    Через str, а не float: 0.1 в двоичной дроби не равно 0.1, а здесь
    это цена.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
