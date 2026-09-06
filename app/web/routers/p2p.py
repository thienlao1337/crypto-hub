"""P2P: объявления, правила ценообразования, журнал.

Роутер тонкий: проверки и решения живут в сервисе, здесь только разбор
формы и переадресация с сообщением.
"""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import P2PPriceRule, User
from app.models.p2p import RULE_LIVE, RULE_OBSERVE
from app.services import p2p_service, payment_verification, tools_service
from app.web import auth, flash
from app.web.templates_env import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/p2p", tags=["p2p"])
settings = get_settings()

PAGE = "/p2p"


@router.get("", response_class=HTMLResponse)
async def ads_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "app/p2p.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "ads": await p2p_service.list_ads(session, user),
            "orders": await p2p_service.list_orders(session, user, limit=30),
            "accounts": await p2p_service.p2p_accounts(session, user),
            "globally_enabled": settings.p2p_enabled,
            "release_configured": payment_verification.is_configured(),
        },
    )


@router.post("/sync")
async def sync_now(
    request: Request,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Подтянуть объявления по кнопке, не дожидаясь фонового прохода."""
    auth.verify_csrf(request, csrf_token)

    accounts = await p2p_service.p2p_accounts(session, user)
    if not accounts:
        flash.error(
            request,
            "Нет подключений с доступом к P2P. Запросите его в разделе «Биржи».",
        )
        return RedirectResponse(PAGE, status_code=303)

    total = 0
    for account in accounts:
        adapter = await p2p_service.build_adapter(session, account)
        try:
            total += await p2p_service.sync_ads(session, account, adapter)
        except Exception as exc:
            logger.warning("Объявления подключения %s не подтянулись: %s", account.id, exc)
            flash.error(request, f"«{account.label}»: {exc}")
        finally:
            await adapter.close()

    await session.commit()
    flash.success(request, f"Объявлений подтянуто: {total}.")
    return RedirectResponse(PAGE, status_code=303)


@router.get("/{ad_id}", response_class=HTMLResponse)
async def ad_page(
    request: Request,
    ad_id: int,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        ad = await p2p_service.get_ad(session, user, ad_id)
    except p2p_service.P2PServiceError as exc:
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    rule = await session.scalar(select(P2PPriceRule).where(P2PPriceRule.ad_id == ad.id))

    return templates.TemplateResponse(
        request,
        "app/p2p_ad.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "ad": ad,
            "rule": rule,
            "events": await p2p_service.recent_events(session, ad),
            "globally_enabled": settings.p2p_enabled,
        },
    )


@router.post("/{ad_id}/rule")
async def save_rule(
    request: Request,
    ad_id: int,
    target_position: int = Form(1),
    step: str = Form("0.1"),
    floor_pct: str = Form(""),
    ceiling_pct: str = Form(""),
    min_change: str = Form("0.05"),
    min_competitor_amount: str = Form(""),
    min_competitor_rate: str = Form(""),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)
    target = f"{PAGE}/{ad_id}"

    try:
        ad = await p2p_service.get_ad(session, user, ad_id)
    except p2p_service.P2PServiceError as exc:
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    try:
        await p2p_service.save_rule(
            session,
            ad,
            target_position=target_position,
            step=tools_service.parse_decimal(step, "шаг обхода"),
            floor_pct=tools_service.parse_decimal(floor_pct, "пол коридора"),
            ceiling_pct=tools_service.parse_decimal(ceiling_pct, "потолок коридора"),
            min_change=tools_service.parse_decimal(min_change, "порог изменения"),
            min_competitor_amount=_optional(min_competitor_amount, "лимит соседа"),
            min_competitor_rate=_optional(min_competitor_rate, "рейтинг соседа"),
        )
    except (p2p_service.P2PServiceError, tools_service.ToolsError) as exc:
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse(target, status_code=303)

    await session.commit()
    flash.success(
        request,
        "Правило сохранено и остановлено. Посмотрите в журнале, какую цену бот "
        "поставил бы по новым настройкам, — и только потом запускайте.",
    )
    return RedirectResponse(target, status_code=303)


@router.post("/{ad_id}/mode")
async def change_mode(
    request: Request,
    ad_id: int,
    mode: str = Form(...),
    confirm: bool = Form(False),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)
    target = f"{PAGE}/{ad_id}"

    ad, rule = await _ad_with_rule(session, user, ad_id)
    if rule is None:
        flash.error(request, "Сначала сохраните правило.")
        return RedirectResponse(target, status_code=303)

    # Боевой режим меняет цену, по которой у клиента реально покупают:
    # одного выбора в списке для этого мало.
    if mode == RULE_LIVE and not confirm:
        flash.error(request, "Чтобы включить боевой режим, подтвердите согласие галочкой.")
        return RedirectResponse(target, status_code=303)

    try:
        await p2p_service.set_mode(session, ad, rule, mode)
    except p2p_service.P2PServiceError as exc:
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse(target, status_code=303)

    await session.commit()
    flash.warn(
        request,
        "Боевой режим включён. Правило остановлено — запустите его отдельно."
        if mode == RULE_LIVE
        else "Режим наблюдения включён.",
    )
    return RedirectResponse(target, status_code=303)


@router.post("/{ad_id}/toggle")
async def toggle_rule(
    request: Request,
    ad_id: int,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)
    target = f"{PAGE}/{ad_id}"

    ad, rule = await _ad_with_rule(session, user, ad_id)
    if rule is None:
        flash.error(request, "Сначала сохраните правило.")
        return RedirectResponse(target, status_code=303)

    await p2p_service.set_active(session, ad, rule, not rule.is_active)
    await session.commit()
    flash.success(request, "Правило запущено." if rule.is_active else "Правило остановлено.")
    return RedirectResponse(target, status_code=303)


async def _ad_with_rule(session: AsyncSession, user: User, ad_id: int):
    ad = await p2p_service.get_ad(session, user, ad_id)
    rule = await session.scalar(select(P2PPriceRule).where(P2PPriceRule.ad_id == ad.id))
    return ad, rule


def _optional(raw: str, field: str):
    return tools_service.parse_decimal(raw, field) if (raw or "").strip() else None
