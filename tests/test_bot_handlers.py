"""Проверки обработчиков бота без Telegram.

Обработчик получает сообщение и сессию, а отвечает вызовом answer().
Этого достаточно, чтобы проверить логику: живой бот добавляет только
транспорт. Полная проверка против Telegram API требует токена и делается
отдельно.
"""

from dataclasses import dataclass, field
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.bot.handlers import common, data
from app.models import Alert, AlertType, Exchange, MarketTicker, Timeframe, User
from app.services import market_service, user_service, watchlist_service
from tests import fakes

ALERT_TYPES = [
    ("price_above", "Цена выше уровня"),
    ("price_below", "Цена ниже уровня"),
]


@dataclass
class FakeUser:
    id: int = 555
    username: str | None = "trader"


@dataclass
class FakeMessage:
    """Минимальная замена aiogram.types.Message."""

    text: str = ""
    from_user: FakeUser = field(default_factory=FakeUser)
    answers: list[str] = field(default_factory=list)

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)

    @property
    def reply(self) -> str:
        assert self.answers, "обработчик ничего не ответил"
        return self.answers[-1]


@dataclass
class FakeCommand:
    args: str | None = None


@pytest_asyncio.fixture
async def setup(session):
    exchange = Exchange(code="bybit", name="Bybit", sort_order=10)
    binance = Exchange(code="binance", name="Binance", sort_order=20)
    session.add_all([exchange, binance])
    session.add(Timeframe(code="5m", label="5 минут", seconds=300, sort_order=20))
    for order, (code, name) in enumerate(ALERT_TYPES):
        session.add(AlertType(code=code, name=name, sort_order=order))
    await session.flush()

    adapter = fakes.FakeAdapter(markets=[fakes.market("BTC/USDT", "BTC", "USDT")])
    for row in (exchange, binance):
        await market_service.sync_markets(session, row, adapter)

    bybit_market = await market_service.get_market(session, exchange.id, "BTC/USDT")
    binance_market = await market_service.get_market(session, binance.id, "BTC/USDT")
    session.add(MarketTicker(market_id=bybit_market.id, last=Decimal("80000"),
                             change_24h_pct=Decimal("1.5")))
    session.add(MarketTicker(market_id=binance_market.id, last=Decimal("80080"),
                             change_24h_pct=Decimal("1.4")))

    user = await user_service.create_user(
        session, email="trader@example.com", password="trader-password-1"
    )
    user.telegram_id = 555
    await session.commit()

    return {"user": user, "market": bybit_market}


# --- Привязка ---


async def test_unlinked_chat_gets_instructions(session):
    message = FakeMessage(text="/start")
    await common.start(message, user=None)

    assert "не привязан" in message.reply
    assert "код привязки" in message.reply


async def test_link_by_code(session, setup):
    """Код отправляют одним сообщением — обработчик ловит числа."""
    stranger = await user_service.create_user(
        session, email="new@example.com", password="new-password-11"
    )
    code = await user_service.issue_telegram_link_code(session, stranger)
    await session.commit()

    message = FakeMessage(text=code, from_user=FakeUser(id=999, username="newbie"))
    await common.link_by_code(message, session=session, user=None)

    assert "привязан" in message.reply
    assert stranger.telegram_id == 999


async def test_wrong_code_is_rejected(session, setup):
    message = FakeMessage(text="00000000", from_user=FakeUser(id=999))
    await common.link_by_code(message, session=session, user=None)

    assert "не найден" in message.reply.lower()


async def test_unlink(session, setup):
    message = FakeMessage(text="/unlink")
    await common.unlink(message, session=session, user=setup["user"])

    assert "отвязан" in message.reply
    assert setup["user"].telegram_id is None


# --- Данные ---


async def test_price_shows_both_exchanges(session, setup):
    message = FakeMessage()
    await data.price(message, FakeCommand(args="btc"), session=session, user=setup["user"])

    assert "BTC/USDT" in message.reply
    assert "bybit" in message.reply
    assert "binance" in message.reply
    # Разница между биржами — то, ради чего их две.
    assert "Разница между биржами" in message.reply


async def test_price_of_unknown_pair(session, setup):
    message = FakeMessage()
    await data.price(message, FakeCommand(args="ЧТОТО"), session=session, user=setup["user"])

    assert "не найдена" in message.reply


async def test_price_without_argument_explains_format(session, setup):
    message = FakeMessage()
    await data.price(message, FakeCommand(args=""), session=session, user=setup["user"])

    assert "/price" in message.reply


async def test_portfolio_without_accounts(session, setup):
    message = FakeMessage()
    await data.portfolio(message, session=session, user=setup["user"])

    assert "не подключены" in message.reply


async def test_commands_require_linked_account(session):
    for handler, args in (
        (data.portfolio, {}),
        (data.signals, {}),
        (data.alerts_list, {}),
    ):
        message = FakeMessage()
        await handler(message, session=session, user=None, **args)
        assert "не привязан" in message.reply


# --- Алерты через бота ---


async def test_create_alert_from_chat(session, setup):
    await watchlist_service.add(session, setup["user"], setup["market"].id)
    await session.commit()

    message = FakeMessage()
    await data.create_alert(
        message, FakeCommand(args="BTC > 70000"), session=session, user=setup["user"]
    )

    assert "сообщу" in message.reply.lower()
    alerts = (await session.execute(select(Alert))).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].params["level"] == "70000"


async def test_create_alert_rejects_bad_format(session, setup):
    message = FakeMessage()
    await data.create_alert(
        message, FakeCommand(args="купи биткоин"), session=session, user=setup["user"]
    )

    assert "/alert" in message.reply
    assert (await session.execute(select(Alert))).scalars().all() == []


async def test_create_alert_for_unknown_pair(session, setup):
    message = FakeMessage()
    await data.create_alert(
        message, FakeCommand(args="NOSUCH > 100"), session=session, user=setup["user"]
    )

    assert "не найдена" in message.reply


async def test_cyrillic_ticker_is_rejected_with_format_hint(session, setup):
    """Тикеры пишутся латиницей.

    Набранное в русской раскладке до поиска пары не доходит — отвечаем
    подсказкой формата, а не «пара не найдена».
    """
    message = FakeMessage()
    await data.create_alert(
        message, FakeCommand(args="ВТС > 100"), session=session, user=setup["user"]
    )

    assert "/alert" in message.reply


async def test_alerts_list_is_empty_at_first(session, setup):
    message = FakeMessage()
    await data.alerts_list(message, session=session, user=setup["user"])

    assert "/alert" in message.reply


async def test_signals_hint_when_empty(session, setup):
    message = FakeMessage()
    await data.signals(message, session=session, user=setup["user"])

    assert "список отслеживания" in message.reply


@pytest.mark.parametrize("text", ["/portfolio", "/signals", "/alerts"])
def test_help_lists_main_commands(text):
    assert text in common.HELP
