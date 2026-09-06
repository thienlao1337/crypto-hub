"""Расчёт цены объявления. Чистая логика, без базы и без сети.

Правило простое на словах: встань на шаг лучше соседа, которого хочешь
обойти, но не выходи за коридор, посчитанный от рыночной цены.

Коридор здесь не украшение. P2P — доска объявлений, и если бот держится
только за соседей, то встретившись с чужим таким же ботом он получает
бесконечный обмен шагами: каждый перебивает другого, пока цена не станет
разорительной. Рынок — единственная точка опоры снаружи этой петли,
поэтому пол и потолок обязательны, а не «желательны».

Стороны считаются одинаково, отличается только направление выгоды. Мы
продаём — покупателю интереснее цена ниже, значит первое место у самого
дешёвого объявления, и обходят соседа вычитанием шага. Мы покупаем —
наоборот.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.exchanges.p2p.base import BoardEntry

SIDE_SELL = "sell"
SIDE_BUY = "buy"

ACTION_REPRICE = "reprice"
ACTION_HOLD = "hold"
ACTION_SKIP = "skip"


@dataclass(frozen=True)
class PriceBand:
    """Коридор допустимых цен, посчитанный от рыночной."""

    low: Decimal
    high: Decimal


@dataclass(frozen=True)
class PriceDecision:
    action: str
    reason: str
    price: Decimal | None = None
    competitor_price: Decimal | None = None
    # Упёрлись в границу коридора — по этому признаку интерфейс отличает
    # «бот работает» от «бот держится за край и место уже проиграно».
    clamped: bool = False


def band(spot: Decimal, floor_pct: Decimal, ceiling_pct: Decimal) -> PriceBand:
    """Границы от рыночной цены.

    Проценты задаются относительно споте одинаково для обеих сторон:
    коридор — это диапазон наценки, а не «выше» и «ниже».
    """
    hundred = Decimal(100)
    first = spot * (hundred + floor_pct) / hundred
    second = spot * (hundred + ceiling_pct) / hundred
    # Порядок процентов не навязываем: перепутанные местами границы —
    # частая опечатка, и разворачивать их молча честнее, чем считать по
    # перевёрнутому коридору.
    return PriceBand(low=min(first, second), high=max(first, second))


def usable_competitors(
    board: list[BoardEntry],
    *,
    exclude_id: str | None = None,
    min_amount: Decimal | None = None,
    min_rate: Decimal | None = None,
) -> list[BoardEntry]:
    """Соседи, за которыми имеет смысл идти.

    Своё объявление отбрасывается обязательно: перебивая себя, бот
    уезжает в пол за несколько проходов и никогда не останавливается.

    Мелочь и аккаунты с низкой долей успешных сделок отбрасываются по
    настройке: объявление на полсотни долларов от вчерашнего аккаунта
    утащит цену вниз, ничего при этом не обслужив.
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
    """Доска в том порядке, в каком её видит контрагент.

    Первым идёт объявление, которое выберут скорее прочих: у продажи —
    самое дешёвое, у покупки — самое дорогое.
    """
    return sorted(board, key=lambda entry: entry.price, reverse=side == SIDE_BUY)


def decide(
    *,
    side: str,
    current_price: Decimal | None,
    spot: Decimal | None,
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
    """Какую цену поставить объявлению."""
    if side not in (SIDE_SELL, SIDE_BUY):
        return PriceDecision(ACTION_SKIP, f"Неизвестная сторона объявления: {side}.")

    if spot is None or spot <= 0:
        # Без рынка нет коридора, а без коридора двигать цену нельзя:
        # именно коридор не даёт боту уехать вслед за соседом в убыток.
        return PriceDecision(
            ACTION_SKIP,
            "Нет рыночной цены — не от чего считать коридор, цену не трогаем.",
        )

    limits = band(spot, floor_pct, ceiling_pct)
    # Край коридора, выгодный нам: продавать дороже, покупать дешевле.
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
    """Прижать к коридору, сравнить с текущей и объяснить решение."""
    clamped_price = min(max(desired, limits.low), limits.high)
    clamped = clamped_price != desired

    if clamped:
        edge = "пол" if clamped_price == limits.low else "потолок"
        # Уточняем, чем именно кончилось дело: «упёрлись в пол» и
        # «встали как хотели» — разные новости для владельца объявления.
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
    """Цена в сообщении без хвоста нулей."""
    if value is None:
        return "—"
    return format(value.normalize(), "f")
