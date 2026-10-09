"""Portfolio, quotes, signals and alerts in the bot.

The handlers are thin: they parse input and call the same services as the web panel.
Numbers must never differ between the bot and the site.
"""

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import formatting
from app.bot.handlers.common import link_hint
from app.models import Exchange, Market, MarketTicker, User
from app.models.market import MARKET_TYPE_SPOT
from app.services import (
    alert_service,
    portfolio_service,
    position_service,
    signal_service,
    watchlist_service,
)

router = Router(name="data")

MAX_ROWS = 12


@router.message(Command("portfolio"))
async def portfolio(message: Message, session: AsyncSession, user: User | None) -> None:
    if user is None:
        await message.answer(link_hint())
        return

    summary = await portfolio_service.build_summary(session, user)
    if not summary.has_accounts:
        await message.answer(
            "Биржи не подключены. Добавьте ключ в панели, в разделе «Биржи»."
        )
        return

    lines = [f"<b>Портфель</b>\n{formatting.money(summary.total_usd)}"]

    if summary.change_24h_pct is not None:
        lines.append(
            f"{formatting.arrow(summary.change_24h_pct)} "
            f"{formatting.percent(summary.change_24h_pct)} за сутки"
        )

    positions = await position_service.list_positions(session, user)
    unrealized = position_service.total_unrealized(positions)
    if unrealized is not None:
        lines.append(
            f"{formatting.arrow(unrealized)} {formatting.money(unrealized)} "
            "нереализованного PnL"
        )

    if summary.by_exchange:
        lines.append("")
        for code, value in summary.by_exchange.items():
            lines.append(f"{code}: {formatting.money(value)}")

    lines.append("")
    for holding in summary.holdings[:MAX_ROWS]:
        value = formatting.money(holding.usd_value) if holding.usd_value else "—"
        share = f" · {holding.share_pct:.1f}%" if holding.share_pct is not None else ""
        lines.append(
            f"<code>{holding.asset_symbol:<6}</code> "
            f"{formatting.number(holding.total)} → {value}{share}"
        )

    if summary.unpriced:
        lines.append("")
        lines.append(
            "Без оценки (нет пары к стейблкоину): " + ", ".join(summary.unpriced)
        )

    await message.answer("\n".join(lines))


@router.message(Command("price"))
async def price(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    user: User | None,
) -> None:
    if user is None:
        await message.answer(link_hint())
        return

    try:
        symbol = formatting.normalize_symbol(command.args or "")
    except formatting.CommandError as exc:
        await message.answer(str(exc))
        return

    rows = (
        await session.execute(
            select(Exchange.code, MarketTicker)
            .select_from(Market)
            .join(Exchange, Exchange.id == Market.exchange_id)
            .join(MarketTicker, MarketTicker.market_id == Market.id)
            .where(Market.symbol == symbol, Market.market_type == MARKET_TYPE_SPOT)
            .order_by(Exchange.sort_order)
        )
    ).all()

    if not rows:
        await message.answer(f"Пара {symbol} не найдена среди подключённых бирж.")
        return

    lines = [f"<b>{symbol}</b>"]
    for code, ticker in rows:
        change = (
            f"  {formatting.arrow(ticker.change_24h_pct)} "
            f"{formatting.percent(ticker.change_24h_pct)}"
            if ticker.change_24h_pct is not None
            else ""
        )
        lines.append(f"{code}: <code>{formatting.number(ticker.last)}</code>{change}")

    # The difference between exchanges is the whole point of having two.
    prices = [ticker.last for _code, ticker in rows if ticker.last]
    if len(prices) > 1:
        low, high = min(prices), max(prices)
        if low > 0:
            spread = (high - low) / low * 100
            lines.append(f"\nРазница между биржами: {spread:.3f}%")

    await message.answer("\n".join(lines))


@router.message(Command("signals"))
async def signals(message: Message, session: AsyncSession, user: User | None) -> None:
    if user is None:
        await message.answer(link_hint())
        return

    rows = await signal_service.recent_signals(session, user_id=user.id, limit=8)
    if not rows:
        await message.answer(
            "Сигналов пока нет. Добавьте пары в список отслеживания в панели — "
            "по ним начнут считаться правила."
        )
        return

    lines = ["<b>Свежие сигналы</b>"]
    for signal, symbol, timeframe_code in rows:
        word = formatting.direction_word(signal.direction)
        lines.append(
            f"\n<b>{symbol}</b> {timeframe_code} — {word} по "
            f"<code>{formatting.number(signal.price)}</code>\n"
            f"<i>{signal.reason}</i>\n"
            f"{formatting.moment(signal.created_at, user)}"
        )

    lines.append("\n<i>Технический анализ, не финансовая рекомендация.</i>")
    await message.answer("\n".join(lines))


@router.message(Command("alerts"))
async def alerts_list(message: Message, session: AsyncSession, user: User | None) -> None:
    if user is None:
        await message.answer(link_hint())
        return

    rows = await alert_service.list_alerts(session, user)
    if not rows:
        await message.answer("Алертов нет. Создать: <code>/alert BTC &gt; 70000</code>")
        return

    lines = ["<b>Ваши алерты</b>"]
    for alert, type_name, symbol in rows[:20]:
        state = "" if alert.is_active else " (выключен)"
        params = " ".join(f"{key}={value}" for key, value in (alert.params or {}).items())
        lines.append(f"{symbol} — {type_name} {params}{state}")

    await message.answer("\n".join(lines))


@router.message(Command("alert"))
async def create_alert(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    user: User | None,
) -> None:
    if user is None:
        await message.answer(link_hint())
        return

    try:
        request = formatting.parse_alert(command.args or "")
        symbol = formatting.normalize_symbol(request.symbol)
    except formatting.CommandError as exc:
        await message.answer(str(exc))
        return

    market = await _find_watched_market(session, user, symbol)
    if market is None:
        await message.answer(
            f"Пара {symbol} не найдена. Проверьте написание или добавьте её "
            "в список отслеживания в панели."
        )
        return

    try:
        await alert_service.create_alert(
            session,
            user,
            market_id=market.id,
            type_code=request.type_code,
            params={"level": str(request.level)},
        )
    except alert_service.AlertError as exc:
        await session.rollback()
        await message.answer(str(exc))
        return

    await session.commit()
    word = "выше" if request.type_code == alert_service.TYPE_PRICE_ABOVE else "ниже"
    await message.answer(
        f"Готово: сообщу, когда {symbol} будет {word} "
        f"<code>{formatting.number(request.level)}</code>."
    )


async def _find_watched_market(
    session: AsyncSession, user: User, symbol: str
) -> Market | None:
    """A pair from the watchlist, otherwise any pair with that symbol.

    Preferring a watched pair is deliberate: the background process keeps data fresh
    only for those.
    """
    watched = await watchlist_service.market_ids(session, user)
    if watched:
        result = await session.execute(
            select(Market).where(
                Market.symbol == symbol,
                Market.id.in_(watched),
                Market.market_type == MARKET_TYPE_SPOT,
            )
        )
        market = result.scalars().first()
        if market is not None:
            return market

    result = await session.execute(
        select(Market)
        .join(MarketTicker, MarketTicker.market_id == Market.id)
        .where(Market.symbol == symbol, Market.market_type == MARKET_TYPE_SPOT)
        .order_by(MarketTicker.quote_volume_24h.desc().nulls_last())
    )
    return result.scalars().first()
