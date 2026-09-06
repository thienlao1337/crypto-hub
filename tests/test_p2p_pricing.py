"""Расчёт цены объявления на P2P.

Здесь проверяется в первую очередь то, чего бот делать не должен: уезжать
за коридор, гоняться за мусорными объявлениями, перебивать сам себя и
двигать цену, когда рынок неизвестен. Ошибка в любом из этих мест не
падает и не логируется — она просто продаёт дешевле, чем нужно.
"""

from decimal import Decimal

import pytest

from app.exchanges.p2p.base import BoardEntry
from app.services import p2p_pricing as pricing

REFERENCE = Decimal(100)


def entry(price: str, *, ad_id: str | None = None, max_amount: str = "100000",
          rate: str | None = None) -> BoardEntry:
    return BoardEntry(
        price=Decimal(price),
        max_amount=Decimal(max_amount),
        completion_rate=Decimal(rate) if rate is not None else None,
        external_id=ad_id,
    )


def decide(side="sell", board=None, current="105", **overrides):
    params = {
        "side": side,
        "current_price": Decimal(current) if current is not None else None,
        "reference": REFERENCE,
        "board": board or [],
        "target_position": 1,
        "step": Decimal("0.1"),
        "floor_pct": Decimal(1),      # не дешевле 101
        "ceiling_pct": Decimal(10),   # не дороже 110
        "min_change": Decimal("0.05"),
    }
    params.update(overrides)
    return pricing.decide(**params)


# --- Коридор ---


def test_band_is_computed_from_spot():
    limits = pricing.band(Decimal(100), Decimal(1), Decimal(10))

    assert limits.low == Decimal(101)
    assert limits.high == Decimal(110)


def test_swapped_percentages_do_not_invert_the_band():
    """Перепутать местами пол и потолок — частая опечатка."""
    limits = pricing.band(Decimal(100), Decimal(10), Decimal(1))

    assert limits.low == Decimal(101)
    assert limits.high == Decimal(110)


def test_floor_stops_the_race_to_the_bottom():
    """Главная защита: сосед ниже пола — место уступаем, но не идём за ним."""
    decision = decide(board=[entry("100.5")])

    assert decision.action == pricing.ACTION_REPRICE
    assert decision.price == Decimal(101), "ниже пола коридора уходить нельзя"
    assert decision.clamped is True
    assert "уступаем" in decision.reason


def test_ceiling_holds_when_competitors_are_expensive():
    decision = decide(board=[entry("200")], current="105")

    assert decision.price == Decimal(110)
    assert decision.clamped is True


# --- Обход соседа ---


def test_undercuts_the_first_seller_by_a_step():
    decision = decide(board=[entry("106"), entry("108")])

    assert decision.action == pricing.ACTION_REPRICE
    assert decision.price == Decimal("105.9")
    assert decision.competitor_price == Decimal(106)


def test_buy_side_outbids_upward():
    """У покупки выгода в обратную сторону: перебиваем прибавкой."""
    decision = decide(
        side="buy", board=[entry("102"), entry("101")], current="100",
        floor_pct=Decimal(-10), ceiling_pct=Decimal(10),
    )

    assert decision.price == Decimal("102.1")


def test_target_position_two_ignores_the_leader():
    """Второе место дешевле первого и часто выгоднее."""
    decision = decide(board=[entry("106"), entry("107"), entry("108")], target_position=2)

    assert decision.competitor_price == Decimal(107)
    assert decision.price == Decimal("106.9")


def test_empty_board_goes_to_the_profitable_edge():
    decision = decide(board=[])

    assert decision.price == Decimal(110), "продавать без конкурентов — по потолку"
    assert "Соседей" in decision.reason


def test_fewer_competitors_than_target_uses_the_edge():
    decision = decide(board=[entry("106")], target_position=3)

    assert decision.price == Decimal(110)


# --- Кого не считаем соседом ---


def test_own_ad_is_never_a_competitor():
    """Перебивая себя, бот уезжает в пол за несколько проходов."""
    decision = decide(board=[entry("106", ad_id="мой")], exclude_id="мой")

    assert decision.competitor_price is None
    assert decision.price == Decimal(110)


def test_small_competitors_are_ignored():
    """Объявление на полсотни утащит цену, ничего не обслужив."""
    decision = decide(
        board=[entry("102", max_amount="50"), entry("107", max_amount="100000")],
        min_competitor_amount=Decimal(1000),
    )

    assert decision.competitor_price == Decimal(107)


def test_unreliable_competitors_are_ignored():
    decision = decide(
        board=[entry("102", rate="40"), entry("107", rate="99")],
        min_competitor_rate=Decimal(90),
    )

    assert decision.competitor_price == Decimal(107)


def test_competitor_without_rating_is_kept():
    """Неизвестный рейтинг — не повод выбрасывать: площадка его не всегда отдаёт."""
    decision = decide(board=[entry("107", rate=None)], min_competitor_rate=Decimal(90))

    assert decision.competitor_price == Decimal(107)


# --- Когда не двигаем ---


def test_no_reference_price_means_no_move():
    """Без опоры нет коридора, а без коридора двигать цену опасно."""
    decision = decide(board=[entry("106")], reference=None)

    assert decision.action == pricing.ACTION_SKIP
    assert "коридор" in decision.reason


def test_change_below_threshold_is_held():
    """Каждое обновление — запрос к площадке, и лимит не бесконечный."""
    decision = decide(board=[entry("106")], current="105.88", min_change=Decimal("0.05"))

    assert decision.action == pricing.ACTION_HOLD
    assert decision.price == Decimal("105.88")


def test_change_above_threshold_is_applied():
    decision = decide(board=[entry("106")], current="105.5", min_change=Decimal("0.05"))

    assert decision.action == pricing.ACTION_REPRICE
    assert decision.price == Decimal("105.9")


def test_unknown_side_is_refused():
    assert decide(side="боком").action == pricing.ACTION_SKIP


# --- Устойчивость к встречному боту ---


def test_two_bots_cannot_push_each_other_below_the_floor():
    """Обмен шагами упирается в пол, а не продолжается бесконечно.

    Подставляем цену «соседа» на шаг ниже нашей — как вёл бы себя чужой
    бот с той же логикой — и смотрим, где остановится обмен шагами.
    """
    price = Decimal(110)
    # За круг цена падает на два шага, так что кругов нужно с запасом:
    # важно не только что она не улетит, но и что остановится.
    for _ in range(100):
        decision = decide(board=[entry(str(price - Decimal("0.1")))], current=str(price))
        assert decision.price >= Decimal(101), "пол коридора обязан держать"
        price = decision.price

    assert price == Decimal(101), "цена обязана осесть ровно на полу"

    # И остаться там: сосед продолжает демпинговать, мы больше не идём.
    settled = decide(board=[entry("100.5")], current=str(price))
    assert settled.action == pricing.ACTION_HOLD
    assert settled.price == Decimal(101)


@pytest.mark.parametrize("position", [0, -5])
def test_nonsense_position_falls_back_to_first(position):
    decision = decide(board=[entry("106"), entry("108")], target_position=position)

    assert decision.competitor_price == Decimal(106)


# --- Опорная цена ---


def test_reference_is_the_middle_of_the_board():
    board = [entry("100"), entry("105"), entry("110")]

    assert pricing.reference_price(board) == Decimal(105)


def test_single_absurd_ad_does_not_move_the_reference():
    """Среднее такое объявление утащило бы, а с ним и весь коридор."""
    board = [entry("100"), entry("105"), entry("110"), entry("100000")]
    sane = [entry("100"), entry("105"), entry("110")]

    # Медиана четырёх — между двумя средними, но всё ещё рядом с рынком.
    assert pricing.reference_price(board) < Decimal(120)
    assert pricing.reference_price(sane) == Decimal(105)


def test_reference_without_board_is_unknown():
    """Пустая доска — не повод выдумывать опору."""
    assert pricing.reference_price([]) is None
