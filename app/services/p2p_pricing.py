"""Ad price calculation. Pure logic, no database and no network.

The rule is simple in words: get one step ahead of the neighbour you want to beat, but
don't leave the corridor computed from the reference price.

The corridor isn't decoration here. P2P is an ad board, and if the bot only follows its
neighbours, then when it meets someone else's similar bot it gets an endless exchange of
steps: each outbids the other until the price becomes ruinous. The corridor is the only
thing that stops this loop, which is why floor and ceiling are mandatory, not "nice to
have".

The reference price is the board median, not the spot quote. There's no USDT/RUB spot
pair on the exchange, but any pair on any marketplace has a median. It's also stable:
two bots outbidding each other pull down the tail of the board but barely move the
middle, so the floor beneath them stays put. Once the client gets merchant status, the
anchor can be switched to the marketplace's own reference price - that's a one-function
change.

Both sides are computed the same way; only the direction of advantage differs. When we
sell, a buyer prefers a lower price, so first place goes to the cheapest ad and we beat
a neighbour by subtracting the step. When we buy, it's the opposite.
"""

from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from app.exchanges.p2p.base import BoardEntry

SIDE_SELL = "sell"
SIDE_BUY = "buy"

ACTION_REPRICE = "reprice"
ACTION_HOLD = "hold"
ACTION_SKIP = "skip"


@dataclass(frozen=True)
class PriceBand:
    """The corridor of allowed prices, computed from the reference."""

    low: Decimal
    high: Decimal


@dataclass(frozen=True)
class PriceDecision:
    action: str
    reason: str
    price: Decimal | None = None
    competitor_price: Decimal | None = None
    # Hit the edge of the corridor - the UI uses this flag to tell "the bot is
    # working" from "the bot is clinging to the edge and the position is
    # already lost".
    clamped: bool = False


def reference_price(board: list[BoardEntry]) -> Decimal | None:
    """The anchor: the middle of the board.

    Median, not mean: one ad with an absurd price shifts the mean and the whole corridor
    with it, but leaves the middle alone.
    """
    prices = [entry.price for entry in board if entry.price > 0]
    if not prices:
        return None
    return Decimal(str(median(sorted(prices))))


def band(reference: Decimal, floor_pct: Decimal, ceiling_pct: Decimal) -> PriceBand:
    """Bounds from the reference price.

    Percentages are relative to the anchor the same way for both sides: the corridor is
    a markup range, not "above" and "below".
    """
    hundred = Decimal(100)
    first = reference * (hundred + floor_pct) / hundred
    second = reference * (hundred + ceiling_pct) / hundred
    # We don't enforce the order of the percentages: swapped bounds are a
    # common typo, and silently flipping them is more honest than computing
    # with an inverted corridor.
    return PriceBand(low=min(first, second), high=max(first, second))


def usable_competitors(
    board: list[BoardEntry],
    *,
    exclude_id: str | None = None,
    min_amount: Decimal | None = None,
    min_rate: Decimal | None = None,
) -> list[BoardEntry]:
    """Neighbours worth following.

    Our own ad is always dropped: by outbidding itself the bot sinks to the floor within
    a few passes and never stops.

    Small ads and accounts with a low share of completed trades are dropped per the
    settings: a fifty-dollar ad from yesterday's account would drag the price down
    without serving anyone.
    """
    result = []
    for entry in board:
        if exclude_id is not None and entry.external_id == exclude_id:
            continue
        if min_amount is not None and (entry.max_amount or Decimal(0)) < min_amount:
            continue
        if min_rate is not None and entry.completion_rate is not None:
            if entry.completion_rate < min_rate:
                continue
        result.append(entry)
    return result


def rank(board: list[BoardEntry], side: str) -> list[BoardEntry]:
    """The board in the order the counterparty sees it.

    First comes the ad most likely to be picked: the cheapest when selling, the most
    expensive when buying.
    """
    return sorted(board, key=lambda entry: entry.price, reverse=side == SIDE_BUY)


def decide(
    *,
    side: str,
    current_price: Decimal | None,
    reference: Decimal | None,
    board: list[BoardEntry],
    target_position: int,
    step: Decimal,
    floor_pct: Decimal,
    ceiling_pct: Decimal,
    min_change: Decimal,
    exclude_id: str | None = None,
    min_competitor_amount: Decimal | None = None,
    min_competitor_rate: Decimal | None = None,
) -> PriceDecision:
    """What price to set on the ad."""
    if side not in (SIDE_SELL, SIDE_BUY):
        return PriceDecision(ACTION_SKIP, f"Неизвестная сторона объявления: {side}.")

    if reference is None or reference <= 0:
        # Without an anchor there's no corridor, and without a corridor the
        # price must not move: the corridor is exactly what stops the bot from
        # following a neighbour into a loss.
        return PriceDecision(
            ACTION_SKIP,
            "Нет опорной цены — не от чего считать коридор, цену не трогаем.",
        )

    limits = band(reference, floor_pct, ceiling_pct)
    # The corridor edge that favours us: sell higher, buy lower.
    best_edge = limits.high if side == SIDE_SELL else limits.low

    competitors = rank(
        usable_competitors(
            board,
            exclude_id=exclude_id,
            min_amount=min_competitor_amount,
            min_rate=min_competitor_rate,
        ),
        side,
    )

    position = max(1, target_position)
    if len(competitors) < position:
        return _finish(
            side=side,
            current_price=current_price,
            desired=best_edge,
            limits=limits,
            min_change=min_change,
            competitor_price=None,
            note=(
                "Соседей на этом месте нет — встаём у выгодного края коридора."
                if not competitors
                else f"Объявлений меньше {position} — встаём у выгодного края коридора."
            ),
        )

    target = competitors[position - 1]
    desired = target.price - step if side == SIDE_SELL else target.price + step

    return _finish(
        side=side,
        current_price=current_price,
        desired=desired,
        limits=limits,
        min_change=min_change,
        competitor_price=target.price,
        note=f"Обходим объявление на {position} месте по цене {_num(target.price)}",
    )


def _finish(
    *,
    side: str,
    current_price: Decimal | None,
    desired: Decimal,
    limits: PriceBand,
    min_change: Decimal,
    competitor_price: Decimal | None,
    note: str,
) -> PriceDecision:
    """Clamp to the corridor, compare with the current price and explain the decision."""
    clamped_price = min(max(desired, limits.low), limits.high)
    clamped = clamped_price != desired

    if clamped:
        edge = "пол" if clamped_price == limits.low else "потолок"
        # Spell out how exactly it ended: "hit the floor" and "landed where we
        # wanted" are different news for the ad owner.
        if side == SIDE_SELL and clamped_price == limits.low:
            note += f". Ниже нельзя — упёрлись в {edge} коридора, место уступаем"
        elif side == SIDE_BUY and clamped_price == limits.high:
            note += f". Выше нельзя — упёрлись в {edge} коридора, место уступаем"
        else:
            note += f". Прижато к {edge} коридора"

    if current_price is not None and abs(clamped_price - current_price) < min_change:
        return PriceDecision(
            ACTION_HOLD,
            f"{note}. Разница с текущей ценой меньше порога {_num(min_change)} — "
            "объявление не трогаем.",
            price=current_price,
            competitor_price=competitor_price,
            clamped=clamped,
        )

    return PriceDecision(
        ACTION_REPRICE,
        f"{note}. Новая цена {_num(clamped_price)}.",
        price=clamped_price,
        competitor_price=competitor_price,
        clamped=clamped,
    )


def _num(value: Decimal | None) -> str:
    """A price in the message without trailing zeros."""
    if value is None:
        return "—"
    return format(value.normalize(), "f")
