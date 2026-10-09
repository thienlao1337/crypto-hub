"""P2P: connecting, syncing ads and applying the rule.

The service's job is to tie together the marketplace, the rule and the log. The price
calculation itself lives in p2p_pricing and knows nothing about the database or the
network: that's where it is tested.

Everything the bot decides goes into the log - both when the price was moved and when it
was left alone. The ad trades for real money, and the question "why were we one percent
cheaper overnight" must have an answer in the database.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exchanges.p2p import build_adapter as build_p2p_adapter
from app.exchanges.p2p.base import P2PAccessDenied, P2PAdapter, P2PError
from app.models import (
    Exchange,
    ExchangeAccount,
    P2PAd,
    P2POrder,
    P2PPriceEvent,
    P2PPriceRule,
    User,
)
from app.models.exchange import KEY_STATUS_OK
from app.models.p2p import (
    AD_OFFLINE,
    AD_ONLINE,
    EVENT_ERROR,
    EVENT_HELD,
    EVENT_MODE,
    EVENT_REPRICED,
    EVENT_SKIPPED,
    RULE_LIVE,
    RULE_OBSERVE,
)
from app.services import exchange_keys_service as keys_service
from app.services import p2p_pricing as pricing
from app.services import payment_verification

logger = logging.getLogger(__name__)
settings = get_settings()

MODES = (RULE_OBSERVE, RULE_LIVE)


class P2PServiceError(Exception):
    """The rule can't be saved or applied in this form."""


@dataclass
class AdView:
    """An ad together with its rule and marketplace - for the on-screen list."""

    ad: P2PAd
    rule: P2PPriceRule | None
    exchange: str


# --- Connecting ---


async def build_adapter(session: AsyncSession, account: ExchangeAccount) -> P2PAdapter:
    exchange = await session.get(Exchange, account.exchange_id)
    api_key, api_secret = keys_service.decrypt_credentials(account)
    return build_p2p_adapter(exchange.code, api_key, api_secret, account.is_testnet)


async def verify_access(
    session: AsyncSession, account: ExchangeAccount, adapter: P2PAdapter
) -> bool:
    """Ask the marketplace whether P2P is open to the key.

    Same as with trading: the permission is confirmed by the marketplace, not by a
    checkbox in the form. If it didn't confirm, the answer is no, even if the user is
    sure otherwise.
    """
    access = await adapter.check_access()
    account.allow_p2p = access.is_allowed
    if not access.is_allowed:
        account.last_error = (access.error or "")[:1000] or None
    await session.flush()
    return access.is_allowed


async def p2p_accounts(session: AsyncSession, user: User) -> list[ExchangeAccount]:
    """Connections the marketplace confirmed P2P access for."""
    result = await session.execute(
        select(ExchangeAccount).where(
            ExchangeAccount.user_id == user.id,
            ExchangeAccount.requested_p2p.is_(True),
            ExchangeAccount.allow_p2p.is_(True),
            ExchangeAccount.status == KEY_STATUS_OK,
        )
    )
    return list(result.scalars())


# --- Ads ---


async def sync_ads(
    session: AsyncSession, account: ExchangeAccount, adapter: P2PAdapter
) -> int:
    """Refresh the mirror of our ads.

    An ad that disappeared from the marketplace is marked closed, not deleted: the rule
    and the log are attached to it, and there's no reason to lose history because an ad
    was unpublished.
    """
    ads = await adapter.fetch_my_ads()
    seen: set[str] = set()

    for info in ads:
        seen.add(info.external_id)
        ad = await _get_ad_by_external(session, account.id, info.external_id)
        if ad is None:
            ad = P2PAd(exchange_account_id=account.id, external_id=info.external_id)
            session.add(ad)

        ad.side = info.side
        ad.asset = info.asset
        ad.fiat = info.fiat
        ad.price = info.price
        ad.quantity = info.quantity
        ad.min_amount = info.min_amount
        ad.max_amount = info.max_amount
        ad.status = info.status
        ad.payment_methods = {"methods": info.payment_methods} if info.payment_methods else None
        ad.synced_at = datetime.now(timezone.utc)
        ad.raw = info.raw

    existing = await session.execute(
        select(P2PAd).where(P2PAd.exchange_account_id == account.id)
    )
    for ad in existing.scalars():
        if ad.external_id not in seen and ad.status != "closed":
            ad.status = "closed"

    await session.flush()
    return len(ads)


async def list_ads(session: AsyncSession, user: User) -> list[AdView]:
    rows = await session.execute(
        select(P2PAd, P2PPriceRule, Exchange.code)
        .join(ExchangeAccount, ExchangeAccount.id == P2PAd.exchange_account_id)
        .join(Exchange, Exchange.id == ExchangeAccount.exchange_id)
        .outerjoin(P2PPriceRule, P2PPriceRule.ad_id == P2PAd.id)
        .where(ExchangeAccount.user_id == user.id)
        .order_by(P2PAd.id)
    )
    return [AdView(ad=ad, rule=rule, exchange=code) for ad, rule, code in rows]


async def get_ad(session: AsyncSession, user: User, ad_id: int) -> P2PAd:
    result = await session.execute(
        select(P2PAd)
        .join(ExchangeAccount, ExchangeAccount.id == P2PAd.exchange_account_id)
        .where(P2PAd.id == ad_id, ExchangeAccount.user_id == user.id)
    )
    ad = result.scalar_one_or_none()
    if ad is None:
        raise P2PServiceError("Объявление не найдено.")
    return ad


# --- Rule ---


def validate_rule(
    *,
    target_position: int,
    step: Decimal,
    floor_pct: Decimal,
    ceiling_pct: Decimal,
    min_change: Decimal,
) -> None:
    """Validate the rule before saving.

    A degenerate corridor isn't nitpicking: with equal bounds the bot has no room to
    maneuver, and with inverted ones it gets pinned to a single point - and that would
    be impossible to figure out from its behaviour.
    """
    if target_position < 1:
        raise P2PServiceError("Место в списке — целое число от 1.")
    if step <= 0:
        raise P2PServiceError("Шаг обхода должен быть больше нуля.")
    if min_change <= 0:
        raise P2PServiceError("Порог изменения должен быть больше нуля.")
    if floor_pct >= ceiling_pct:
        raise P2PServiceError(
            "Пол коридора должен быть ниже потолка — иначе цене некуда двигаться."
        )


async def save_rule(
    session: AsyncSession,
    ad: P2PAd,
    *,
    target_position: int,
    step: Decimal,
    floor_pct: Decimal,
    ceiling_pct: Decimal,
    min_change: Decimal,
    min_competitor_amount: Decimal | None = None,
    min_competitor_rate: Decimal | None = None,
) -> P2PPriceRule:
    """Create or update a rule. Always stopping it.

    A new rule is also created in observe mode: a rule that starts moving the price
    right after saving is configuration in the dark - the person hasn't seen a single
    decision by the bot and has already handed it the ad.

    Editing an existing rule stops it too. Shifting the corridor of a rule running live
    means immediately repricing to bounds nobody has checked. Let the person take a look
    and start it themselves.
    """
    validate_rule(
        target_position=target_position,
        step=step,
        floor_pct=floor_pct,
        ceiling_pct=ceiling_pct,
        min_change=min_change,
    )

    rule = await _get_rule(session, ad.id)
    created = rule is None
    if rule is None:
        rule = P2PPriceRule(ad_id=ad.id, mode=RULE_OBSERVE, is_active=False)
        session.add(rule)

    rule.target_position = target_position
    rule.step = step
    rule.floor_pct = floor_pct
    rule.ceiling_pct = ceiling_pct
    rule.min_change = min_change
    rule.min_competitor_amount = min_competitor_amount
    rule.min_competitor_rate = min_competitor_rate
    rule.is_active = False

    await session.flush()
    await journal(
        session, ad, EVENT_MODE,
        "Правило создано в режиме наблюдения и остановлено."
        if created
        else "Правило изменено и остановлено — запустите его заново, когда проверите.",
    )
    return rule


async def set_mode(session: AsyncSession, ad: P2PAd, rule: P2PPriceRule, mode: str) -> None:
    """Change the rule mode.

    As with auto-trading, changing the mode always stops the rule: starting it is a
    separate deliberate action, not a side effect of configuration.
    """
    if mode not in MODES:
        raise P2PServiceError("Неизвестный режим правила.")

    rule.mode = mode
    rule.is_active = False
    await journal(
        session, ad, EVENT_MODE,
        "Режим: боевой — бот будет менять цену объявления."
        if mode == RULE_LIVE
        else "Режим: наблюдение — бот считает и пишет в журнал, цену не трогает.",
    )
    await session.flush()


async def set_active(session: AsyncSession, ad: P2PAd, rule: P2PPriceRule, active: bool) -> None:
    rule.is_active = active
    await journal(
        session, ad, EVENT_MODE, "Правило запущено." if active else "Правило остановлено."
    )
    await session.flush()


# --- Applying ---


async def apply_rule(
    session: AsyncSession,
    ad: P2PAd,
    rule: P2PPriceRule,
    adapter: P2PAdapter,
) -> P2PPriceEvent | None:
    """Compute the price from the board and apply it if needed."""
    if not settings.p2p_enabled:
        return await journal(
            session, ad, EVENT_SKIPPED,
            "P2P выключен глобально (P2P_ENABLED).",
            collapse_repeats=True,
        )

    if not rule.is_active:
        return None

    try:
        board = await adapter.fetch_board(side=ad.side, asset=ad.asset, fiat=ad.fiat)
    except P2PAccessDenied as exc:
        return await journal(session, ad, EVENT_ERROR, str(exc), collapse_repeats=True)
    except P2PError as exc:
        return await journal(
            session, ad, EVENT_ERROR, f"Доска недоступна: {exc}", collapse_repeats=True
        )

    reference = pricing.reference_price(board)
    decision = pricing.decide(
        side=ad.side,
        current_price=ad.price,
        reference=reference,
        board=board,
        target_position=rule.target_position,
        step=rule.step,
        floor_pct=rule.floor_pct,
        ceiling_pct=rule.ceiling_pct,
        min_change=rule.min_change,
        exclude_id=ad.external_id,
        min_competitor_amount=rule.min_competitor_amount,
        min_competitor_rate=rule.min_competitor_rate,
    )

    if decision.action == pricing.ACTION_SKIP:
        return await journal(
            session, ad, EVENT_SKIPPED, decision.reason,
            competitor_price=decision.competitor_price, spot_price=reference,
            collapse_repeats=True,
        )

    if decision.action == pricing.ACTION_HOLD:
        return await journal(
            session, ad, EVENT_HELD, decision.reason,
            price_before=ad.price, price_after=ad.price,
            competitor_price=decision.competitor_price, spot_price=reference,
            collapse_repeats=True,
        )

    if rule.mode == RULE_OBSERVE:
        return await journal(
            session, ad, EVENT_HELD,
            f"Наблюдение: поставил бы {_num(decision.price)}. {decision.reason}",
            price_before=ad.price, price_after=decision.price,
            competitor_price=decision.competitor_price, spot_price=reference,
            collapse_repeats=True,
        )

    try:
        await adapter.update_ad_price(ad.external_id, decision.price)
    except P2PError as exc:
        return await journal(
            session, ad, EVENT_ERROR, f"Площадка отклонила изменение цены: {exc}",
            price_before=ad.price, price_after=decision.price,
            competitor_price=decision.competitor_price, spot_price=reference,
        )

    previous, ad.price = ad.price, decision.price
    rule.last_applied_at = datetime.now(timezone.utc)

    return await journal(
        session, ad, EVENT_REPRICED, decision.reason,
        price_before=previous, price_after=decision.price,
        competitor_price=decision.competitor_price, spot_price=reference,
    )


# --- Orders ---


async def sync_orders(
    session: AsyncSession, account: ExchangeAccount, adapter: P2PAdapter
) -> int:
    """Refresh the mirror of orders on our ads.

    Read-only: the panel shows what's going on, and the decision to release funds is
    made by a person.
    """
    orders = await adapter.fetch_orders()
    ads = {
        ad.external_id: ad.id
        for ad in (
            await session.execute(
                select(P2PAd).where(P2PAd.exchange_account_id == account.id)
            )
        ).scalars()
    }

    for info in orders:
        order = await _get_order(session, account.id, info.external_id)
        if order is None:
            order = P2POrder(
                exchange_account_id=account.id, external_id=info.external_id
            )
            session.add(order)

        order.side = info.side
        order.asset = info.asset
        order.fiat = info.fiat
        order.status = info.status
        order.amount = info.amount
        order.fiat_amount = info.fiat_amount
        order.price = info.price
        order.counterparty = info.counterparty
        order.paid_at = info.paid_at
        order.ad_id = ads.get((info.raw or {}).get("itemId") or "", order.ad_id)
        order.synced_at = datetime.now(timezone.utc)
        order.raw = info.raw

    await session.flush()
    return len(orders)


async def list_orders(
    session: AsyncSession, user: User, *, limit: int = 100
) -> list[P2POrder]:
    result = await session.execute(
        select(P2POrder)
        .join(ExchangeAccount, ExchangeAccount.id == P2POrder.exchange_account_id)
        .where(ExchangeAccount.user_id == user.id)
        .order_by(P2POrder.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def release_order(
    session: AsyncSession,
    order: P2POrder,
    adapter: P2PAdapter,
    *,
    verifier=None,
) -> P2POrder:
    """Release crypto for an order.

    Release is only possible after the money's arrival is confirmed by its source - a
    bank or payment gateway. The "paid" mark on the marketplace is not such a
    confirmation: the buyer sets it, and the marketplace doesn't verify it.

    Until a verification provider is configured, automatic release is impossible by
    design, and that's not a temporary limitation: releasing on an unconfirmed claim
    means handing money to anyone who pressed a button.
    """
    if order.released_at is not None:
        raise P2PServiceError("Средства по этому заказу уже отпущены.")

    verifier = verifier or payment_verification.get_verifier()
    check = await verifier.verify(
        amount=order.fiat_amount or Decimal(0),
        currency=order.fiat,
        reference=order.external_id,
    )

    if not check.is_confirmed:
        raise P2PServiceError(check.reason or "Поступление денег не подтверждено.")

    await adapter.release_order(order.external_id)

    order.released_at = datetime.now(timezone.utc)
    order.release_reason = (
        f"Подтверждено источником платежа: {check.reason or 'приход найден'}."
    )
    await session.flush()
    return order


async def _get_order(
    session: AsyncSession, account_id: int, external_id: str
) -> P2POrder | None:
    result = await session.execute(
        select(P2POrder).where(
            P2POrder.exchange_account_id == account_id,
            P2POrder.external_id == external_id,
        )
    )
    return result.scalar_one_or_none()


# --- Log ---


async def journal(
    session: AsyncSession,
    ad: P2PAd,
    event_type: str,
    message: str,
    *,
    price_before: Decimal | None = None,
    price_after: Decimal | None = None,
    competitor_price: Decimal | None = None,
    spot_price: Decimal | None = None,
    collapse_repeats: bool = False,
) -> P2PPriceEvent | None:
    """Record a decision.

    collapse_repeats is for "do nothing" decisions. The rule is recalculated once a
    minute, and in a calm market that's fifteen hundred identical rows a day per ad: a
    log where you can't find the one important entry isn't a log. A consecutive repeat
    of the same decision with the same explanation isn't written, while the first
    occurrence and any change are.
    """
    if collapse_repeats:
        last = await _last_event(session, ad)
        if last is not None and last.event_type == event_type and last.message == message:
            return None

    event = P2PPriceEvent(
        ad_id=ad.id,
        event_type=event_type,
        message=message,
        price_before=price_before,
        price_after=price_after,
        competitor_price=competitor_price,
        spot_price=spot_price,
    )
    session.add(event)
    await session.flush()
    return event


async def _last_event(session: AsyncSession, ad: P2PAd) -> P2PPriceEvent | None:
    result = await session.execute(
        select(P2PPriceEvent)
        .where(P2PPriceEvent.ad_id == ad.id)
        .order_by(P2PPriceEvent.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def recent_events(
    session: AsyncSession, ad: P2PAd, *, limit: int = 100
) -> list[P2PPriceEvent]:
    result = await session.execute(
        select(P2PPriceEvent)
        .where(P2PPriceEvent.ad_id == ad.id)
        .order_by(P2PPriceEvent.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


# --- Helpers ---


async def _get_ad_by_external(
    session: AsyncSession, account_id: int, external_id: str
) -> P2PAd | None:
    result = await session.execute(
        select(P2PAd).where(
            P2PAd.exchange_account_id == account_id, P2PAd.external_id == external_id
        )
    )
    return result.scalar_one_or_none()


async def _get_rule(session: AsyncSession, ad_id: int) -> P2PPriceRule | None:
    result = await session.execute(select(P2PPriceRule).where(P2PPriceRule.ad_id == ad_id))
    return result.scalar_one_or_none()


def _num(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return format(value.normalize(), "f")
