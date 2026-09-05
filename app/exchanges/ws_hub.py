"""Мультиплексор WebSocket-подписок на биржи.

Задача одна: сколько бы вкладок ни смотрело на BTC/USDT, к бирже держится
ровно одна подписка на стакан и одна на ленту сделок. Без этого десять
открытых вкладок дают десять соединений, и биржа начинает ограничивать
или разрывать их.

Устройство простое: на каждый ключ (биржа, пара, канал) заводится задача,
которая читает поток и рассылает данные подписчикам через очереди. Когда
последний подписчик уходит, задача живёт ещё немного — перезагрузка
страницы не должна приводить к переподключению к бирже, — и только потом
закрывается.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import ccxt.pro as ccxtpro

logger = logging.getLogger(__name__)

CHANNEL_ORDER_BOOK = "orderbook"
CHANNEL_TRADES = "trades"

# Биржи принимают не любую глубину, а фиксированный набор значений
# (у Bybit для спота это 1, 50, 200). Запрашиваем допустимую, а
# показываем столько строк, сколько помещается в панель.
ORDER_BOOK_REQUEST_DEPTH = 50
ORDER_BOOK_DEPTH = 15
TRADES_KEEP = 30

# Сколько держать подписку без слушателей. Перезагрузка страницы
# укладывается в этот промежуток, и переподключаться не приходится.
LINGER_SECONDS = 20
# Пауза после сбоя, чтобы не долбить биржу в цикле.
RETRY_DELAY = 3.0
# Размер очереди подписчика: если браузер не успевает читать, старые
# кадры выбрасываются — в стакане нужен свежий срез, а не история.
QUEUE_SIZE = 4


@dataclass
class _Stream:
    key: tuple[str, str, str]
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None
    closer: asyncio.TimerHandle | None = None
    # Последний кадр отдаётся новому подписчику сразу, чтобы он не ждал
    # следующего обновления с пустым экраном.
    last_payload: dict[str, Any] | None = None


class MarketStreamHub:
    def __init__(self) -> None:
        self._streams: dict[tuple[str, str, str], _Stream] = {}
        self._clients: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    # --- Публичный интерфейс ---

    async def subscribe(
        self, exchange_code: str, symbol: str, channel: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Поток обновлений по одной паре и каналу."""
        key = (exchange_code, symbol, channel)
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)

        stream = await self._attach(key, queue)
        if stream.last_payload is not None:
            yield stream.last_payload

        try:
            while True:
                yield await queue.get()
        finally:
            await self._detach(key, queue)

    async def close(self) -> None:
        """Погасить все подписки. Вызывается при остановке приложения."""
        async with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()

        for stream in streams:
            if stream.closer is not None:
                stream.closer.cancel()
            if stream.task is not None:
                stream.task.cancel()

        for client in list(self._clients.values()):
            try:
                await client.close()
            except Exception:
                logger.debug("Ошибка при закрытии соединения с биржей", exc_info=True)
        self._clients.clear()

    # --- Внутреннее ---

    async def _attach(self, key, queue: asyncio.Queue) -> _Stream:
        async with self._lock:
            stream = self._streams.get(key)
            if stream is None:
                stream = _Stream(key=key)
                self._streams[key] = stream

            if stream.closer is not None:
                # Подписчик вернулся раньше, чем истёк запас — отменяем закрытие.
                stream.closer.cancel()
                stream.closer = None

            stream.subscribers.add(queue)
            if stream.task is None or stream.task.done():
                stream.task = asyncio.create_task(self._run(stream))
            return stream

    async def _detach(self, key, queue: asyncio.Queue) -> None:
        async with self._lock:
            stream = self._streams.get(key)
            if stream is None:
                return

            stream.subscribers.discard(queue)
            if stream.subscribers or stream.closer is not None:
                return

            loop = asyncio.get_running_loop()
            stream.closer = loop.call_later(
                LINGER_SECONDS, lambda: asyncio.create_task(self._stop_if_idle(key))
            )

    async def _stop_if_idle(self, key) -> None:
        async with self._lock:
            stream = self._streams.get(key)
            if stream is None or stream.subscribers:
                return
            self._streams.pop(key, None)
            stream.closer = None
            task = stream.task

        if task is not None:
            task.cancel()
        await self._close_client_if_unused(key[0])

    async def _close_client_if_unused(self, exchange_code: str) -> None:
        async with self._lock:
            still_used = any(k[0] == exchange_code for k in self._streams)
            client = None if still_used else self._clients.pop(exchange_code, None)

        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("Ошибка при закрытии %s", exchange_code, exc_info=True)

    async def _client(self, exchange_code: str):
        async with self._lock:
            client = self._clients.get(exchange_code)
            if client is None:
                factory = getattr(ccxtpro, exchange_code)
                client = factory({"enableRateLimit": True, "options": {"defaultType": "spot"}})
                self._clients[exchange_code] = client
            return client

    async def _run(self, stream: _Stream) -> None:
        exchange_code, symbol, channel = stream.key
        client = await self._client(exchange_code)

        while True:
            try:
                if channel == CHANNEL_ORDER_BOOK:
                    payload = await self._read_order_book(client, symbol)
                else:
                    payload = await self._read_trades(client, symbol)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("Поток %s %s %s прервался: %s", exchange_code, symbol, channel, exc)
                self._broadcast(
                    stream,
                    {"type": channel, "error": f"Поток прерван: {exc}"[:200]},
                )
                await asyncio.sleep(RETRY_DELAY)
                continue

            stream.last_payload = payload
            self._broadcast(stream, payload)

    async def _read_order_book(self, client, symbol: str) -> dict[str, Any]:
        book = await client.watch_order_book(symbol, ORDER_BOOK_REQUEST_DEPTH)
        return {
            "type": CHANNEL_ORDER_BOOK,
            "bids": [[_num(p), _num(a)] for p, a in book.get("bids", [])[:ORDER_BOOK_DEPTH]],
            "asks": [[_num(p), _num(a)] for p, a in book.get("asks", [])[:ORDER_BOOK_DEPTH]],
            "timestamp": book.get("timestamp"),
        }

    async def _read_trades(self, client, symbol: str) -> dict[str, Any]:
        trades = await client.watch_trades(symbol)
        return {
            "type": CHANNEL_TRADES,
            "trades": [
                {
                    "price": _num(trade.get("price")),
                    "amount": _num(trade.get("amount")),
                    "side": trade.get("side"),
                    "timestamp": trade.get("timestamp"),
                }
                for trade in trades[-TRADES_KEEP:]
            ],
        }

    def _broadcast(self, stream: _Stream, payload: dict[str, Any]) -> None:
        for queue in list(stream.subscribers):
            if queue.full():
                # Медленный клиент не должен тормозить остальных: выкидываем
                # у него самый старый кадр, свежий важнее.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass


def _num(value) -> float | None:
    return None if value is None else float(value)


hub = MarketStreamHub()
