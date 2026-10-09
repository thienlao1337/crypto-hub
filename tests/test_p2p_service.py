"""P2P service: ad sync, rule, log.

As with auto-trading, what's tested first and foremost is what the bot must not do: move
the price with the kill switch off, start working right after a rule is saved, or change
the ad in observe mode.
"""

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.exchanges.p2p.base import AdInfo, BoardEntry, OrderInfo, P2PAccess, P2PError
from app.models import ExchangeAccount, P2PAd, P2POrder, P2PPriceEvent
from app.models.exchange import KEY_STATUS_OK
from app.models.p2p import EVENT_ERROR, EVENT_HELD, EVENT_REPRICED, RULE_LIVE, RULE_OBSERVE
from app.services import exchange_keys_service as keys
from app.services import p2p_service, user_service
from tests import fakes


class FakeP2P:
    """A marketplace with a predefined board that records what it was sent."""

    def __init__(self, ads=None, board=None, access=True, fail_update=False, orders=None):
        self._ads = ads or []
        self._board = board or []
        self._orders = orders or []
        self._access = access
        self.fail_update = fail_update
        self.updates: list[tuple[str, Decimal]] = []
        self.released: list[str] = []
        self.closed = False

    async def check_access(self):
        return P2PAccess(is_allowed=self._access, error=None if self._access else "нет статуса")

    async def fetch_my_ads(self):
        return self._ads

    async def fetch_board(self, *, side, asset, fiat, payment=None):
        return self._board

    async def update_ad_price(self, external_id, price):
        if self.fail_update:
            raise P2PError("площадка отказала")
        self.updates.append((external_id, price))

    async def fetch_orders(self):
        return self._orders

    async def release_order(self, external_id):
        self.released.append(external_id)

    async def close(self):
        self.closed = True


def ad_info(external_id="42", price="101", side="sell") -> AdInfo:
    return AdInfo(
        external_id=external_id,
        side=side,
        asset="USDT",
        fiat="RUB",
        price=Decimal(price),
        quantity=Decimal(500),
        status="online",
    )


def board_entry(price: str, ad_id: str | None = None) -> BoardEntry:
    return BoardEntry(
        price=Decimal(price), max_amount=Decimal(100000), external_id=ad_id
    )


@pytest_asyncio.fixture
async def setup(session, monkeypatch):
    monkeypatch.setattr(p2p_service.settings, "p2p_enabled", True, raising=False)

    from app.models import Exchange

    session.add(Exchange(code="bybit", name="Bybit", sort_order=10))
    await session.flush()

    user = await user_service.create_user(
        session, email="p2p@example.com", password="p2p-password-1"
    )
    await session.flush()

    account = await keys.add_account(
        session, user,
        exchange_code="bybit", api_key="bybit-key-0001", api_secret="secret",
        adapter_factory=fakes.factory_for(fakes.FakeAdapter()),
    )
    account.requested_p2p = True
    account.allow_p2p = True
    account.status = KEY_STATUS_OK
    await session.commit()

    return {"user": user, "account": account}


async def make_ad(session, setup, platform=None, **kwargs) -> P2PAd:
    platform = platform or FakeP2P(ads=[ad_info(**kwargs)])
    await p2p_service.sync_ads(session, setup["account"], platform)
    await session.flush()
    return (await session.execute(select(P2PAd))).scalars().first()


async def make_rule(session, ad, **overrides):
    params = {
        "target_position": 1,
        "step": Decimal("0.1"),
        "floor_pct": Decimal(-2),
        "ceiling_pct": Decimal(5),
        "min_change": Decimal("0.05"),
    }
    params.update(overrides)
    return await p2p_service.save_rule(session, ad, **params)


# --- Access ---


async def test_access_is_confirmed_by_the_platform(session, setup):
    """The permission is confirmed by the marketplace, not by a checkbox in the form."""
    account = setup["account"]
    account.allow_p2p = False
    await session.flush()

    assert await p2p_service.verify_access(session, account, FakeP2P(access=True)) is True
    assert account.allow_p2p is True


async def test_refused_access_is_remembered_with_reason(session, setup):
    account = setup["account"]

    assert await p2p_service.verify_access(session, account, FakeP2P(access=False)) is False
    assert account.allow_p2p is False
    assert "нет статуса" in account.last_error


# --- Ad sync ---


async def test_ads_are_mirrored(session, setup):
    ad = await make_ad(session, setup)

    assert ad.external_id == "42"
    assert ad.price == Decimal(101)
    assert ad.asset == "USDT" and ad.fiat == "RUB"


async def test_sync_is_idempotent(session, setup):
    platform = FakeP2P(ads=[ad_info()])
    await p2p_service.sync_ads(session, setup["account"], platform)
    await p2p_service.sync_ads(session, setup["account"], platform)
    await session.commit()

    assert len((await session.execute(select(P2PAd))).scalars().all()) == 1


async def test_vanished_ad_is_closed_not_deleted(session, setup):
    """The rule and the log are attached to the ad - no reason to lose them."""
    await make_ad(session, setup)
    await p2p_service.sync_ads(session, setup["account"], FakeP2P(ads=[]))
    await session.commit()

    ad = (await session.execute(select(P2PAd))).scalar_one()
    assert ad.status == "closed"


# --- Rule ---


@pytest.mark.parametrize(
    "params",
    [
        {"target_position": 0},
        {"step": Decimal(0)},
        {"min_change": Decimal(0)},
        # A degenerate corridor: the price has nowhere to move.
        {"floor_pct": Decimal(5), "ceiling_pct": Decimal(5)},
        {"floor_pct": Decimal(5), "ceiling_pct": Decimal(-2)},
    ],
)
async def test_unsafe_rule_is_refused(session, setup, params):
    ad = await make_ad(session, setup)

    with pytest.raises(p2p_service.P2PServiceError):
        await make_rule(session, ad, **params)


async def test_rule_starts_stopped_and_observing(session, setup):
    """A rule that moves the price right after saving is configuration in the dark."""
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)

    assert rule.mode == RULE_OBSERVE
    assert rule.is_active is False


async def test_mode_change_stops_the_rule(session, setup):
    """Starting is a separate action, not a side effect of configuration."""
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_active(session, ad, rule, True)

    await p2p_service.set_mode(session, ad, rule, RULE_LIVE)

    assert rule.mode == RULE_LIVE
    assert rule.is_active is False


# --- Applying ---


async def test_observe_mode_writes_but_does_not_touch_the_ad(session, setup):
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_active(session, ad, rule, True)

    platform = FakeP2P(board=[board_entry("100"), board_entry("102"), board_entry("104")])
    event = await p2p_service.apply_rule(session, ad, rule, platform)
    await session.commit()

    assert platform.updates == [], "в наблюдении цену трогать нельзя"
    assert event.event_type == EVENT_HELD
    assert "Наблюдение" in event.message
    assert ad.price == Decimal(101), "цена объявления осталась прежней"


async def test_live_mode_moves_the_price(session, setup):
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_mode(session, ad, rule, RULE_LIVE)
    await p2p_service.set_active(session, ad, rule, True)

    platform = FakeP2P(board=[board_entry("100"), board_entry("102"), board_entry("104")])
    event = await p2p_service.apply_rule(session, ad, rule, platform)
    await session.commit()

    assert len(platform.updates) == 1
    external_id, price = platform.updates[0]
    assert external_id == "42"
    assert ad.price == price
    assert event.event_type == EVENT_REPRICED
    # The log must show what the decision was based on.
    assert event.competitor_price == Decimal(100)
    assert event.spot_price == Decimal(102), "опора — середина доски"


async def test_global_switch_blocks_everything(session, setup, monkeypatch):
    """A switched-off kill switch beats any rule settings."""
    monkeypatch.setattr(p2p_service.settings, "p2p_enabled", False, raising=False)

    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_mode(session, ad, rule, RULE_LIVE)
    await p2p_service.set_active(session, ad, rule, True)

    platform = FakeP2P(board=[board_entry("100")])
    await p2p_service.apply_rule(session, ad, rule, platform)
    await session.commit()

    assert platform.updates == []


async def test_stopped_rule_does_nothing(session, setup):
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_mode(session, ad, rule, RULE_LIVE)

    platform = FakeP2P(board=[board_entry("100")])

    assert await p2p_service.apply_rule(session, ad, rule, platform) is None
    assert platform.updates == []


async def test_platform_refusal_is_journalled_not_swallowed(session, setup):
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_mode(session, ad, rule, RULE_LIVE)
    await p2p_service.set_active(session, ad, rule, True)

    platform = FakeP2P(board=[board_entry("100"), board_entry("102")], fail_update=True)
    event = await p2p_service.apply_rule(session, ad, rule, platform)
    await session.commit()

    assert event.event_type == EVENT_ERROR
    assert ad.price == Decimal(101), "цена в базе не меняется, если площадка отказала"


async def test_own_ad_on_the_board_is_not_chased(session, setup):
    """Otherwise the bot outbids itself and sinks to the floor."""
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_mode(session, ad, rule, RULE_LIVE)
    await p2p_service.set_active(session, ad, rule, True)

    platform = FakeP2P(
        board=[board_entry("101", ad_id="42"), board_entry("103"), board_entry("105")]
    )
    event = await p2p_service.apply_rule(session, ad, rule, platform)
    await session.commit()

    assert event.competitor_price == Decimal(103), "своё объявление соседом не считаем"


async def test_journal_keeps_the_full_picture(session, setup):
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_mode(session, ad, rule, RULE_LIVE)
    await p2p_service.set_active(session, ad, rule, True)
    await p2p_service.apply_rule(
        session, ad, rule, FakeP2P(board=[board_entry("100"), board_entry("102")])
    )
    await session.commit()

    events = await p2p_service.recent_events(session, ad)
    kinds = [event.event_type for event in events]

    assert EVENT_REPRICED in kinds
    # Configuration and starting are logged too: "who turned this on" is asked
    # just as often as "why did the price move".
    assert any("Правило" in event.message for event in events)


async def test_editing_a_running_rule_stops_it(session, setup):
    """Shifting the corridor on the fly means repricing to unchecked bounds."""
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_mode(session, ad, rule, RULE_LIVE)
    await p2p_service.set_active(session, ad, rule, True)
    assert rule.is_active is True

    await make_rule(session, ad, floor_pct=Decimal(-5))
    await session.commit()

    assert rule.is_active is False, "правка обязана останавливать"
    assert rule.mode == RULE_LIVE, "режим при этом не сбрасываем — его выбирали отдельно"
    assert rule.floor_pct == Decimal(-5)


async def test_repeated_no_op_is_not_journalled_again(session, setup):
    """The rule is evaluated once a minute - fifteen hundred rows a day.

    A log where you can't find the one important entry isn't a log. A consecutive repeat
    of the same decision isn't written.
    """
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_active(session, ad, rule, True)

    platform = FakeP2P(board=[board_entry("100"), board_entry("102"), board_entry("104")])

    first = await p2p_service.apply_rule(session, ad, rule, platform)
    second = await p2p_service.apply_rule(session, ad, rule, platform)
    third = await p2p_service.apply_rule(session, ad, rule, platform)
    await session.commit()

    assert first is not None, "первое решение записать обязаны"
    assert second is None and third is None

    events = await p2p_service.recent_events(session, ad)
    observations = [e for e in events if "Наблюдение" in e.message]
    assert len(observations) == 1


async def test_changed_decision_is_journalled(session, setup):
    """Repeats are collapsed, changes are not: the board moved - an entry is needed."""
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad)
    await p2p_service.set_active(session, ad, rule, True)

    await p2p_service.apply_rule(
        session, ad, rule, FakeP2P(board=[board_entry("100"), board_entry("102")])
    )
    changed = await p2p_service.apply_rule(
        session, ad, rule, FakeP2P(board=[board_entry("99"), board_entry("103")])
    )
    await session.commit()

    assert changed is not None


async def test_price_moves_are_never_collapsed(session, setup):
    """A price change is always recorded, however many times in a row it repeats."""
    ad = await make_ad(session, setup)
    rule = await make_rule(session, ad, min_change=Decimal("0.001"))
    await p2p_service.set_mode(session, ad, rule, RULE_LIVE)
    await p2p_service.set_active(session, ad, rule, True)

    for price in ("102", "103", "104"):
        board = [board_entry(price), board_entry(str(Decimal(price) + Decimal(2)))]
        event = await p2p_service.apply_rule(session, ad, rule, FakeP2P(board=board))
        assert event is not None and event.event_type == EVENT_REPRICED
    await session.commit()

    events = await p2p_service.recent_events(session, ad)
    assert len([e for e in events if e.event_type == EVENT_REPRICED]) == 3


# --- Orders and releasing funds ---


def order_info(external_id="ord-1", status="paid") -> OrderInfo:
    return OrderInfo(
        external_id=external_id,
        side="sell",
        asset="USDT",
        fiat="RUB",
        status=status,
        amount=Decimal(100),
        fiat_amount=Decimal(9700),
        price=Decimal(97),
        counterparty="Покупатель",
    )


class ConfirmingVerifier:
    async def verify(self, *, amount, currency, reference, since=None):
        from app.services.payment_verification import PaymentCheck

        return PaymentCheck(is_confirmed=True, reason="перевод найден", matched_amount=amount)


async def test_orders_are_mirrored(session, setup):
    platform = FakeP2P(orders=[order_info()])

    count = await p2p_service.sync_orders(session, setup["account"], platform)
    await session.commit()

    assert count == 1
    order = (await session.execute(select(P2POrder))).scalar_one()
    assert order.fiat_amount == Decimal(9700)
    assert order.counterparty == "Покупатель"


async def test_order_sync_is_idempotent(session, setup):
    platform = FakeP2P(orders=[order_info()])
    await p2p_service.sync_orders(session, setup["account"], platform)
    await p2p_service.sync_orders(session, setup["account"], platform)
    await session.commit()

    assert len((await session.execute(select(P2POrder))).scalars().all()) == 1


async def test_release_is_refused_without_a_configured_verifier(session, setup):
    """The "paid" mark is set by the buyer - it's not a confirmation of arrival.

    Releasing on it means handing money to anyone who pressed a button.
    """
    platform = FakeP2P(orders=[order_info()])
    await p2p_service.sync_orders(session, setup["account"], platform)
    order = (await session.execute(select(P2POrder))).scalar_one()

    with pytest.raises(p2p_service.P2PServiceError) as info:
        await p2p_service.release_order(session, order, platform)

    assert "не настроена" in str(info.value)
    assert platform.released == [], "до площадки дело доходить не должно"
    assert order.released_at is None


async def test_release_goes_through_when_payment_is_confirmed(session, setup):
    platform = FakeP2P(orders=[order_info()])
    await p2p_service.sync_orders(session, setup["account"], platform)
    order = (await session.execute(select(P2POrder))).scalar_one()

    await p2p_service.release_order(
        session, order, platform, verifier=ConfirmingVerifier()
    )
    await session.commit()

    assert platform.released == ["ord-1"]
    assert order.released_at is not None
    assert "Подтверждено источником платежа" in order.release_reason


async def test_release_twice_is_refused(session, setup):
    """A repeated release is paying out the same money twice."""
    platform = FakeP2P(orders=[order_info()])
    await p2p_service.sync_orders(session, setup["account"], platform)
    order = (await session.execute(select(P2POrder))).scalar_one()

    await p2p_service.release_order(session, order, platform, verifier=ConfirmingVerifier())

    with pytest.raises(p2p_service.P2PServiceError):
        await p2p_service.release_order(
            session, order, platform, verifier=ConfirmingVerifier()
        )

    assert platform.released == ["ord-1"]
