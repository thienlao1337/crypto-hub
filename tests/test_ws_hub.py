"""Проверки мультиплексора потоков.

Главное свойство: сколько бы вкладок ни смотрело на пару, к бирже идёт
одна подписка. Без этого биржа начинает резать соединения.
"""

import asyncio

import pytest

from app.exchanges import ws_hub


class FakeWsClient:
    """Подделка ccxt.pro: отдаёт заготовленные кадры по очереди."""

    def __init__(self) -> None:
        self.order_book_calls = 0
        self.trade_calls = 0
        self.closed = False
        self.fail_once = False

    async def watch_order_book(self, symbol, limit):
        self.order_book_calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("биржа отвалилась")
        await asyncio.sleep(0.01)
        return {
            "bids": [[100.0, 1.0], [99.0, 2.0]],
            "asks": [[101.0, 1.5]],
            "timestamp": 1_700_000_000_000,
        }

    async def watch_trades(self, symbol):
        self.trade_calls += 1
        await asyncio.sleep(0.01)
        return [{"price": 100.5, "amount": 0.1, "side": "buy", "timestamp": 1}]

    async def close(self):
        self.closed = True


class FakeCcxtPro:
    """Заглушка модуля ccxt.pro: отдаёт один и тот же клиент."""

    def __init__(self, client: FakeWsClient) -> None:
        self._client = client

    def __getattr__(self, name):
        return lambda *args, **kwargs: self._client


@pytest.fixture
def hub(monkeypatch):
    """Свежий мультиплексор с поддельной биржей.

    Подменяется именно модуль ccxt.pro, а не метод _client: иначе тест
    обошёл бы регистрацию соединения, на которой держится закрытие.
    """
    client = FakeWsClient()
    monkeypatch.setattr(ws_hub, "ccxtpro", FakeCcxtPro(client))

    instance = ws_hub.MarketStreamHub()
    instance.test_client = client
    return instance


async def take(stream, count: int) -> list[dict]:
    """Забрать несколько кадров и отпустить подписку."""
    frames = []
    async for payload in stream:
        frames.append(payload)
        if len(frames) >= count:
            break
    return frames


async def test_order_book_frames_are_normalized(hub):
    frames = await take(hub.subscribe("bybit", "BTC/USDT", ws_hub.CHANNEL_ORDER_BOOK), 1)

    assert frames[0]["type"] == "orderbook"
    assert frames[0]["bids"][0] == [100.0, 1.0]
    assert frames[0]["asks"][0] == [101.0, 1.5]
    await hub.close()


async def test_trade_frames_are_normalized(hub):
    frames = await take(hub.subscribe("bybit", "BTC/USDT", ws_hub.CHANNEL_TRADES), 1)

    assert frames[0]["type"] == "trades"
    assert frames[0]["trades"][0]["side"] == "buy"
    await hub.close()


async def test_two_subscribers_share_one_exchange_connection(hub):
    """Ради этого мультиплексор и написан."""
    first = hub.subscribe("bybit", "BTC/USDT", ws_hub.CHANNEL_ORDER_BOOK)
    second = hub.subscribe("bybit", "BTC/USDT", ws_hub.CHANNEL_ORDER_BOOK)

    frames = await asyncio.gather(take(first, 2), take(second, 2))

    assert len(frames[0]) == 2 and len(frames[1]) == 2
    assert len(hub._streams) == 1, "на пару и канал заведён один поток"
    await hub.close()


async def test_new_subscriber_gets_last_frame_immediately(hub):
    """Открывший вкладку не должен смотреть в пустой стакан до обновления."""
    await take(hub.subscribe("bybit", "BTC/USDT", ws_hub.CHANNEL_ORDER_BOOK), 1)

    stream = hub.subscribe("bybit", "BTC/USDT", ws_hub.CHANNEL_ORDER_BOOK)
    first = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert first["type"] == "orderbook"
    await stream.aclose()
    await hub.close()


async def test_stream_lingers_after_last_subscriber(hub):
    """Перезагрузка страницы не должна рвать подписку на бирже."""
    await take(hub.subscribe("bybit", "BTC/USDT", ws_hub.CHANNEL_ORDER_BOOK), 1)

    assert len(hub._streams) == 1
    assert not hub.test_client.closed, "соединение держится про запас"
    await hub.close()


async def test_failure_is_reported_and_retried(hub, monkeypatch):
    monkeypatch.setattr(ws_hub, "RETRY_DELAY", 0.01)
    hub.test_client.fail_once = True

    frames = await take(hub.subscribe("bybit", "BTC/USDT", ws_hub.CHANNEL_ORDER_BOOK), 2)

    assert "error" in frames[0], "о сбое сообщаем сразу"
    assert frames[1]["type"] == "orderbook", "после паузы поток продолжается"
    await hub.close()


async def test_close_releases_everything(hub):
    await take(hub.subscribe("bybit", "BTC/USDT", ws_hub.CHANNEL_ORDER_BOOK), 1)
    await hub.close()

    assert hub._streams == {}
    assert hub.test_client.closed


async def test_slow_subscriber_does_not_block_others(hub):
    """Медленный клиент теряет старые кадры, а не тормозит поток."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=ws_hub.QUEUE_SIZE)
    stream = ws_hub._Stream(key=("bybit", "BTC/USDT", ws_hub.CHANNEL_ORDER_BOOK))
    stream.subscribers.add(queue)

    for index in range(ws_hub.QUEUE_SIZE + 3):
        hub._broadcast(stream, {"type": "orderbook", "n": index})

    assert queue.qsize() == ws_hub.QUEUE_SIZE
    # В очереди остались последние кадры: свежий срез важнее истории.
    newest = queue.get_nowait()
    assert newest["n"] == 3
