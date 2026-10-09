"""P2P ad price calculation.

What's tested first and foremost is what the bot must not do: leave the corridor, chase
junk ads, outbid itself, or move the price when the market is unknown. A mistake in any
of these places doesn't crash and isn't logged - it just sells cheaper than it should.
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
        "floor_pct": Decimal(1),      # not cheaper than 101
        "ceiling_pct": Decimal(10),   # not more expensive than 110
        "min_change": Decimal("0.05"),
    }
    params.update(overrides)
    return pricing.decide(**params)


# --- Corridor ---


def test_band_is_computed_from_spot():
    limits = pricing.band(Decimal(100), Decimal(1), Decimal(10))

    assert limits.low == Decimal(101)
    assert limits.high == Decimal(110)


def test_swapped_percentages_do_not_invert_the_band():
    """Swapping floor and ceiling is a common typo."""
    limits = pricing.band(Decimal(100), Decimal(10), Decimal(1))

    assert limits.low == Decimal(101)
    assert limits.high == Decimal(110)


def test_floor_stops_the_race_to_the_bottom():
    """Key safeguard: a neighbour below the floor - give up the spot, don't follow."""
    decision = decide(board=[entry("100.5")])

    assert decision.action == pricing.ACTION_REPRICE
    assert decision.price == Decimal(101), "ниже пола коридора уходить нельзя"
    assert decision.clamped is True
    assert "уступаем" in decision.reason


def test_ceiling_holds_when_competitors_are_expensive():
    decision = decide(board=[entry("200")], current="105")

    assert decision.price == Decimal(110)
    assert decision.clamped is True


# --- Beating the neighbour ---


def test_undercuts_the_first_seller_by_a_step():
    decision = decide(board=[entry("106"), entry("108")])

    assert decision.action == pricing.ACTION_REPRICE
    assert decision.price == Decimal("105.9")
    assert decision.competitor_price == Decimal(106)


def test_buy_side_outbids_upward():
    """When buying, the advantage goes the other way: we outbid by adding."""
    decision = decide(
        side="buy", board=[entry("102"), entry("101")], current="100",
        floor_pct=Decimal(-10), ceiling_pct=Decimal(10),
    )

    assert decision.price == Decimal("102.1")


def test_target_position_two_ignores_the_leader():
    """Second place is cheaper than first and often more profitable."""
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


# --- Who doesn't count as a neighbour ---


def test_own_ad_is_never_a_competitor():
    """By outbidding itself the bot sinks to the floor within a few passes."""
    decision = decide(board=[entry("106", ad_id="мой")], exclude_id="мой")

    assert decision.competitor_price is None
    assert decision.price == Decimal(110)


def test_small_competitors_are_ignored():
    """A fifty-dollar ad would drag the price down without serving anyone."""
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
    """An unknown rating isn't a reason to drop an ad: the marketplace doesn't always report it."""
    decision = decide(board=[entry("107", rate=None)], min_competitor_rate=Decimal(90))

    assert decision.competitor_price == Decimal(107)


# --- When we don't move ---


def test_no_reference_price_means_no_move():
    """No anchor means no corridor, and moving the price without one is dangerous."""
    decision = decide(board=[entry("106")], reference=None)

    assert decision.action == pricing.ACTION_SKIP
    assert "коридор" in decision.reason


def test_change_below_threshold_is_held():
    """Every update is a request to the marketplace, and the limit isn't infinite."""
    decision = decide(board=[entry("106")], current="105.88", min_change=Decimal("0.05"))

    assert decision.action == pricing.ACTION_HOLD
    assert decision.price == Decimal("105.88")


def test_change_above_threshold_is_applied():
    decision = decide(board=[entry("106")], current="105.5", min_change=Decimal("0.05"))

    assert decision.action == pricing.ACTION_REPRICE
    assert decision.price == Decimal("105.9")


def test_unknown_side_is_refused():
    assert decide(side="боком").action == pricing.ACTION_SKIP


# --- Resilience against a counter-bot ---


def test_two_bots_cannot_push_each_other_below_the_floor():
    """The exchange of steps hits the floor instead of going on forever.

    We set the "neighbour" price one step below ours - the way someone else's bot with
    the same logic would behave - and see where the exchange of steps stops.
    """
    price = Decimal(110)
    # Each round the price drops by two steps, so we need rounds with a margin:
    # what matters is not only that it doesn't run away, but that it stops.
    for _ in range(100):
        decision = decide(board=[entry(str(price - Decimal("0.1")))], current=str(price))
        assert decision.price >= Decimal(101), "пол коридора обязан держать"
        price = decision.price

    assert price == Decimal(101), "цена обязана осесть ровно на полу"

    # And stay there: the neighbour keeps undercutting, we no longer follow.
    settled = decide(board=[entry("100.5")], current=str(price))
    assert settled.action == pricing.ACTION_HOLD
    assert settled.price == Decimal(101)


@pytest.mark.parametrize("position", [0, -5])
def test_nonsense_position_falls_back_to_first(position):
    decision = decide(board=[entry("106"), entry("108")], target_position=position)

    assert decision.competitor_price == Decimal(106)


# --- Reference price ---


def test_reference_is_the_middle_of_the_board():
    board = [entry("100"), entry("105"), entry("110")]

    assert pricing.reference_price(board) == Decimal(105)


def test_single_absurd_ad_does_not_move_the_reference():
    """Such an ad would drag the mean, and the whole corridor with it."""
    board = [entry("100"), entry("105"), entry("110"), entry("100000")]
    sane = [entry("100"), entry("105"), entry("110")]

    # The median of four is between the two middle ones, but still close to the market.
    assert pricing.reference_price(board) < Decimal(120)
    assert pricing.reference_price(sane) == Decimal(105)


def test_reference_without_board_is_unknown():
    """An empty board is no reason to make up an anchor."""
    assert pricing.reference_price([]) is None
