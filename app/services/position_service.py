"""Открытые позиции и нереализованный PnL.

Спотовая биржа не отдаёт «позиции»: она отдаёт баланс и историю сделок.
Среднюю цену входа приходится собирать самим — проходом по сделкам от
старых к новым с усреднением по стоимости. Тот же проход попутно считает
реализованный PnL каждой продажи: без него в истории видно, что и почём
продано, но не видно, заработали на этом или потеряли.

Честность цифры упирается в полноту истории. Биржи отдают ограниченный
период, поэтому продажа монеты, купленной до начала этого периода,
выглядит как продажа из ниоткуда, а купленная давно позиция — как
меньшая, чем есть на балансе. Оба случая не замалчиваются: позиция
помечается cost_basis_complete = False, и интерфейс показывает цифру с
оговоркой, а не выдаёт неполный расчёт за точный.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Asset,
    Balance,
    Exchange,
    ExchangeAccount,
    Market,
    MarketTicker,
    Position,
    Trade,
    User,
)
from app.models.portfolio import SIDE_BUY
from app.services import market_service

logger = logging.getLogger(__name__)

# Насколько позиция, собранная из сделок, может недотягивать до баланса,
# прежде чем это считается признаком неполной истории. Небольшой зазор
# нужен всегда: комиссии списываются с монеты, переводы между кошельками
# биржи в историю сделок не попадают.
BALANCE_TOLERANCE = Decimal("0.99")


@dataclass
class WalkResult:
    """Итог прохода по сделкам одной пары."""

    amount: Decimal
    cost: Decimal
    opened_at: datetime | None
    complete: bool
    # По одному значению на сделку, в том же порядке: реализованный PnL
    # продажи или None для покупки.
    realized: list[Decimal | None] = field(default_factory=list)

    @property
    def entry_price(self) -> Decimal:
        return self.cost / self.amount if self.amount > 0 else Decimal(0)


@dataclass
class PositionView:
    """Строка таблицы открытых позиций."""

    symbol: str
    exchange: str
    amount: Decimal
    entry_price: Decimal
    mark_price: Decimal | None
    cost_usd: Decimal | None
    value_usd: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pct: Decimal | None
    cost_basis_complete: bool


def walk_trades(
    trades: list[Trade], *, base_asset_id: int, quote_asset_id: int
) -> WalkResult:
    """Пройти сделки одной пары и собрать открытую позицию.

    Функция чистая: ничего не пишет и не читает из базы, поэтому её
    можно проверять на выдуманных сделках без биржи и без сессии.

    Комиссия учитывается, только если списана валютой котировки (растёт
    стоимость входа) или самой монетой (уменьшается полученное
    количество). Комиссию третьей монетой — скидочный BNB и подобное —
    пересчитывать не по чему, и она игнорируется: занизить цену входа
    молчаливой подстановкой курса хуже, чем не учесть.
    """
    amount = Decimal(0)
    cost = Decimal(0)
    opened_at: datetime | None = None
    complete = True
    realized: list[Decimal | None] = []

    for trade in trades:
        qty = trade.amount or Decimal(0)
        gross = trade.cost if trade.cost is not None else qty * trade.price
        fee = trade.fee or Decimal(0)
        fee_quote = fee if trade.fee_asset_id == quote_asset_id else Decimal(0)
        fee_base = fee if trade.fee_asset_id == base_asset_id else Decimal(0)

        if trade.side == SIDE_BUY:
            received = qty - fee_base
            if received <= 0:
                realized.append(None)
                continue
            if amount <= 0:
                opened_at = trade.executed_at
            amount += received
            cost += gross + fee_quote
            realized.append(None)
            continue

        if amount <= 0 or qty <= 0:
            # Продано то, чего в истории нет: покупка осталась за
            # пределами периода, который отдаёт биржа.
            complete = False
            realized.append(None)
            continue

        entry = cost / amount
        sold = min(qty, amount)
        if sold < qty:
            complete = False

        # Выручку берём пропорционально закрытой части: если продано
        # больше, чем мы знаем, оставшийся хвост нам не принадлежит.
        proceeds = (gross - fee_quote) * (sold / qty)
        realized.append(proceeds - entry * sold)

        amount -= sold
        cost -= entry * sold
        if amount <= 0:
            amount = Decimal(0)
            cost = Decimal(0)
            opened_at = None

    return WalkResult(
        amount=amount,
        cost=cost,
        opened_at=opened_at,
        complete=complete,
        realized=realized,
    )


async def rebuild_positions(session: AsyncSession, account: ExchangeAccount) -> int:
    """Пересобрать позиции одного подключения из истории сделок.

    Пересчёт идёт целиком, а не приращением: досинхронизация может
    принести сделку задним числом, и тогда средняя цена входа меняется
    у всей последующей цепочки.
    """
    markets = (
        await session.execute(
            select(Market.id, Market.base_asset_id, Market.quote_asset_id)
            .join(Trade, Trade.market_id == Market.id)
            .where(Trade.exchange_account_id == account.id)
            .distinct()
        )
    ).all()

    open_amount_by_asset: dict[int, Decimal] = {}
    open_markets: dict[int, int] = {}
    kept: set[int] = set()

    for market_id, base_asset_id, quote_asset_id in markets:
        trades = list(
            (
                await session.execute(
                    select(Trade)
                    .where(
                        Trade.exchange_account_id == account.id,
                        Trade.market_id == market_id,
                    )
                    .order_by(Trade.executed_at, Trade.id)
                )
            ).scalars()
        )

        result = walk_trades(
            trades, base_asset_id=base_asset_id, quote_asset_id=quote_asset_id
        )
        for trade, pnl in zip(trades, result.realized):
            trade.realized_pnl = pnl

        position = await _get_position(session, account.id, market_id)

        if result.amount <= 0:
            # Позиция закрыта. Существующую строку не удаляем — по ней
            # видно, что пара торговалась и сейчас в ней ничего нет, — но
            # и новую под нулевой остаток не заводим: список закрытых
            # позиций дублировал бы историю сделок.
            if position is not None:
                position.amount = Decimal(0)
                position.is_open = False
                position.mark_price = None
                position.unrealized_pnl = None
            continue

        if position is None:
            position = Position(
                exchange_account_id=account.id,
                market_id=market_id,
                side=SIDE_BUY,
            )
            session.add(position)

        position.amount = result.amount
        position.entry_price = result.entry_price
        position.opened_at = result.opened_at
        position.is_open = True
        position.cost_basis_complete = result.complete

        kept.add(market_id)
        open_markets[market_id] = base_asset_id
        open_amount_by_asset[base_asset_id] = (
            open_amount_by_asset.get(base_asset_id, Decimal(0)) + result.amount
        )

    await _flag_against_balances(session, account, open_amount_by_asset, open_markets)
    await session.flush()
    return len(kept)


async def mark_positions(session: AsyncSession) -> int:
    """Проставить открытым позициям текущую цену и нереализованный PnL.

    Вызывается сразу после обновления котировок, чтобы оценка и срез
    цен, по которому она сделана, были из одного момента.
    """
    prices = await market_service.build_usd_price_map(session)

    rows = (
        await session.execute(
            select(Position, MarketTicker.last, Asset.symbol)
            .join(Market, Market.id == Position.market_id)
            .join(Asset, Asset.id == Market.quote_asset_id)
            .outerjoin(MarketTicker, MarketTicker.market_id == Position.market_id)
            .where(Position.is_open.is_(True))
        )
    ).all()

    updated = 0
    for position, last, quote_symbol in rows:
        if last is None or last <= 0:
            continue

        position.mark_price = last
        rate = prices.get(quote_symbol)
        if rate is None:
            # Пара к неоцениваемой котировке: цену показать можем,
            # доллары — нет.
            position.unrealized_pnl = None
            continue

        position.unrealized_pnl = (last - position.entry_price) * position.amount * rate
        updated += 1

    await session.flush()
    return updated


async def list_positions(session: AsyncSession, user: User) -> list[PositionView]:
    """Открытые позиции пользователя, от крупных к мелким."""
    account_ids = [
        account_id
        for (account_id,) in await session.execute(
            select(ExchangeAccount.id).where(ExchangeAccount.user_id == user.id)
        )
    ]
    if not account_ids:
        return []

    prices = await market_service.build_usd_price_map(session)

    rows = (
        await session.execute(
            select(Position, Market.symbol, Exchange.code, Asset.symbol)
            .join(Market, Market.id == Position.market_id)
            .join(Exchange, Exchange.id == Market.exchange_id)
            .join(Asset, Asset.id == Market.quote_asset_id)
            .where(
                Position.exchange_account_id.in_(account_ids),
                Position.is_open.is_(True),
                Position.amount > 0,
            )
        )
    ).all()

    views: list[PositionView] = []
    for position, symbol, exchange_code, quote_symbol in rows:
        rate = prices.get(quote_symbol)
        cost_usd = position.entry_price * position.amount * rate if rate else None

        value_usd = None
        pct = None
        if position.mark_price is not None and rate is not None:
            value_usd = position.mark_price * position.amount * rate
        if position.mark_price is not None and position.entry_price > 0:
            pct = (
                (position.mark_price - position.entry_price)
                / position.entry_price
                * Decimal(100)
            )

        views.append(
            PositionView(
                symbol=symbol,
                exchange=exchange_code,
                amount=position.amount,
                entry_price=position.entry_price,
                mark_price=position.mark_price,
                cost_usd=cost_usd,
                value_usd=value_usd,
                unrealized_pnl=position.unrealized_pnl,
                unrealized_pct=pct,
                cost_basis_complete=position.cost_basis_complete,
            )
        )

    views.sort(key=lambda v: -(v.value_usd or v.cost_usd or Decimal(0)))
    return views


def total_unrealized(views: list[PositionView]) -> Decimal | None:
    """Сумма нереализованного PnL по оценённым позициям.

    None означает «считать не по чему», а не ноль: пустой портфель и
    портфель без котировок — разные состояния.
    """
    known = [v.unrealized_pnl for v in views if v.unrealized_pnl is not None]
    return sum(known, Decimal(0)) if known else None


# --- Вспомогательное ---


async def _get_position(
    session: AsyncSession, account_id: int, market_id: int
) -> Position | None:
    result = await session.execute(
        select(Position).where(
            Position.exchange_account_id == account_id,
            Position.market_id == market_id,
            Position.side == SIDE_BUY,
        )
    )
    return result.scalar_one_or_none()


async def _flag_against_balances(
    session: AsyncSession,
    account: ExchangeAccount,
    open_amount_by_asset: dict[int, Decimal],
    open_markets: dict[int, int],
) -> None:
    """Сверить позиции с балансом и пометить неполные.

    Позиция, собранная из сделок, должна сходиться с тем, что лежит на
    бирже. Если на балансе монеты заметно больше, чем объясняет история,
    значит часть покупок осталась за пределами отданного периода —
    средняя цена входа описывает не весь остаток, и говорить об этом
    надо прямо.
    """
    if not open_amount_by_asset:
        return

    balances = {
        asset_id: total
        for asset_id, total in await session.execute(
            select(Balance.asset_id, Balance.total).where(
                Balance.exchange_account_id == account.id,
                Balance.asset_id.in_(open_amount_by_asset),
            )
        )
    }

    incomplete_assets = {
        asset_id
        for asset_id, derived in open_amount_by_asset.items()
        if (balance := balances.get(asset_id)) is not None
        and derived < balance * BALANCE_TOLERANCE
    }
    if not incomplete_assets:
        return

    for market_id, base_asset_id in open_markets.items():
        if base_asset_id not in incomplete_assets:
            continue
        position = await _get_position(session, account.id, market_id)
        if position is not None:
            position.cost_basis_complete = False
