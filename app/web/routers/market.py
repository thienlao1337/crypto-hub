"""Рынок: свечной график, индикаторы и сравнение бирж."""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Form, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, session_scope
from app.exchanges.ccxt_client import CcxtAdapter
from app.exchanges.ws_hub import CHANNEL_ORDER_BOOK, CHANNEL_TRADES, hub
from app.models import Exchange, Market, MarketTicker, User
from app.models.market import MARKET_TYPE_SPOT
from app.services import candle_service, market_service, watchlist_service
from app.web import auth, flash
from app.web.templates_env import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/market", tags=["market"])

DEFAULT_SYMBOL = "BTC/USDT"
DEFAULT_TIMEFRAME = "1h"

# Наборы по умолчанию: то, что чаще всего включают на графике.
DEFAULT_INDICATORS = {"ema": [9, 21], "sma": [], "rsi": 14, "macd": False, "bollinger": False}


def slug_to_symbol(slug: str) -> str:
    """BTC-USDT -> BTC/USDT. В адресе косая черта неудобна."""
    return slug.replace("-", "/").upper()


def symbol_to_slug(symbol: str) -> str:
    return symbol.replace("/", "-")


@router.get("", response_class=HTMLResponse)
async def market_index(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Список доступных пар с текущими ценами."""
    rows = await session.execute(
        select(Exchange.code, Market.symbol, MarketTicker)
        .select_from(Market)
        .join(Exchange, Exchange.id == Market.exchange_id)
        .join(MarketTicker, MarketTicker.market_id == Market.id)
        .where(
            Market.market_type == MARKET_TYPE_SPOT,
            Market.is_active.is_(True),
            MarketTicker.last.is_not(None),
        )
        .order_by(MarketTicker.quote_volume_24h.desc().nulls_last())
        .limit(60)
    )

    markets = [
        {
            "exchange": code,
            "symbol": symbol,
            "slug": symbol_to_slug(symbol),
            "ticker": ticker,
        }
        for code, symbol, ticker in rows
    ]

    return templates.TemplateResponse(
        request,
        "app/market_index.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "markets": markets,
        },
    )


@router.get("/api/candles")
async def candles_api(
    market_id: int,
    timeframe: str = DEFAULT_TIMEFRAME,
    ema: str = "",
    sma: str = "",
    rsi: int = 0,
    macd: bool = False,
    bollinger: bool = False,
    limit: int = Query(500, ge=50, le=1000),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Свечи и индикаторы для графика.

    Индикаторы считаются на сервере, а не в браузере: те же функции
    используются движком сигналов, и расхождение между тем, что видит
    пользователь, и тем, по чему сработал сигнал, недопустимо.
    """
    market = await session.get(Market, market_id)
    selected = await candle_service.get_timeframe(session, timeframe)
    if market is None or selected is None:
        return JSONResponse({"error": "Пара или таймфрейм не найдены."}, status_code=404)

    exchange = await session.get(Exchange, market.exchange_id)
    candles = await candle_service.candles_for_chart(
        session,
        market,
        selected,
        adapter_factory=lambda: CcxtAdapter(exchange.code),
        limit=limit,
    )
    await session.commit()

    config = {
        "ema": _periods(ema),
        "sma": _periods(sma),
        "rsi": rsi or False,
        "macd": macd,
        "bollinger": bollinger,
    }

    return {
        "symbol": market.symbol,
        "timeframe": selected.code,
        "candles": candle_service.candles_to_chart(candles),
        "indicators": candle_service.compute_indicators(candles, config),
    }


@router.post("/watch/{market_id}")
async def toggle_watch(
    request: Request,
    market_id: int,
    back: str = Form("/market"),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Добавить пару в список отслеживания или убрать из него."""
    auth.verify_csrf(request, csrf_token)

    try:
        watched = await watchlist_service.toggle(session, user, market_id)
    except watchlist_service.WatchlistError as exc:
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse(back, status_code=303)

    await session.commit()
    flash.success(
        request,
        "Пара добавлена в отслеживаемые — по ней пойдут свечи и сигналы."
        if watched
        else "Пара убрана из отслеживаемых.",
    )
    # Открытый редирект недопустим: возвращаем только внутрь панели.
    return RedirectResponse(back if back.startswith("/") else "/market", status_code=303)


@router.get("/{exchange_code}/{slug}", response_class=HTMLResponse)
async def market_page(
    request: Request,
    exchange_code: str,
    slug: str,
    timeframe: str = Query(DEFAULT_TIMEFRAME),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    symbol = slug_to_symbol(slug)
    market = await _find_market(session, exchange_code, symbol)
    if market is None:
        return RedirectResponse("/market", status_code=303)

    timeframes = await candle_service.list_timeframes(session)
    selected = await candle_service.get_timeframe(session, timeframe)
    if selected is None:
        selected = next((t for t in timeframes if t.code == DEFAULT_TIMEFRAME), timeframes[0])

    ticker = await session.get(MarketTicker, market.id)
    comparison = await market_service.compare_across_exchanges(session, symbol)

    return templates.TemplateResponse(
        request,
        "app/market.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "market": market,
            "exchange_code": exchange_code,
            "symbol": symbol,
            "slug": slug,
            "ticker": ticker,
            "timeframes": timeframes,
            "selected_timeframe": selected,
            "comparison": comparison,
            "is_watched": await watchlist_service.is_watched(session, user, market.id),
            "indicators_json": json.dumps(DEFAULT_INDICATORS),
        },
    )


@router.websocket("/stream/{exchange_code}/{slug}")
async def market_stream(websocket: WebSocket, exchange_code: str, slug: str):
    """Стакан и лента сделок в реальном времени.

    Соединение с биржей общее на всех зрителей — им заведует
    мультиплексор, см. app/exchanges/ws_hub.py.
    """
    # Сессионная cookie доступна и в WebSocket: SessionMiddleware стоит
    # выше по стеку. Анонимных сюда не пускаем.
    if not websocket.session.get(auth.SESSION_USER_ID):
        await websocket.close(code=4401)
        return

    symbol = slug_to_symbol(slug)

    async with session_scope() as session:
        market = await _find_market(session, exchange_code, symbol)
    if market is None:
        # Подписываться можно только на пары, которые у нас заведены,
        # иначе адрес превращается в произвольный запрос к бирже.
        await websocket.close(code=4404)
        return

    await websocket.accept()

    async def pump(channel: str) -> None:
        async for payload in hub.subscribe(exchange_code, symbol, channel):
            await websocket.send_json(payload)

    async def wait_for_disconnect() -> None:
        while True:
            await websocket.receive()

    tasks = [
        asyncio.create_task(pump(CHANNEL_ORDER_BOOK)),
        asyncio.create_task(pump(CHANNEL_TRADES)),
        asyncio.create_task(wait_for_disconnect()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _find_market(session: AsyncSession, exchange_code: str, symbol: str) -> Market | None:
    result = await session.execute(
        select(Market)
        .join(Exchange, Exchange.id == Market.exchange_id)
        .where(
            Exchange.code == exchange_code,
            Market.symbol == symbol,
            Market.market_type == MARKET_TYPE_SPOT,
        )
    )
    return result.scalar_one_or_none()


def _periods(raw: str) -> list[int]:
    """Разобрать «9,21» в список периодов, отбросив мусор."""
    periods = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk.isdigit():
            continue
        period = int(chunk)
        if 1 <= period <= 500:
            periods.append(period)
    return periods[:4]
