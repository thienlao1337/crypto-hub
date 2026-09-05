"""Автотрейдинг: стратегии, режимы, журнал.

Все действия, меняющие режим или запускающие стратегию, идут через
сервис — проверки прав и лимитов не должны жить в роутере.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Market, SignalRule, User
from app.services import autotrade_service as auto
from app.services import exchange_keys_service as keys_service
from app.services import tools_service, watchlist_service
from app.web import auth, flash
from app.web.templates_env import templates

router = APIRouter(prefix="/autotrade", tags=["autotrade"])
settings = get_settings()

PAGE = "/autotrade"


@router.get("", response_class=HTMLResponse)
async def strategies_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    accounts = await keys_service.list_accounts(session, user)
    rules = (
        await session.execute(
            select(SignalRule)
            .where(
                SignalRule.is_active.is_(True),
                (SignalRule.user_id == user.id) | (SignalRule.user_id.is_(None)),
            )
            .order_by(SignalRule.id)
        )
    ).scalars().all()

    watched = await watchlist_service.list_items(session, user)

    return templates.TemplateResponse(
        request,
        "app/autotrade.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "strategies": await auto.list_strategies(session, user),
            "accounts": accounts,
            "rules": rules,
            "watched": watched,
            "globally_enabled": settings.autotrade_enabled,
        },
    )


@router.post("")
async def create_strategy(
    request: Request,
    name: str = Form(...),
    signal_rule_id: int = Form(...),
    exchange_account_id: int = Form(...),
    market_id: int = Form(...),
    position_size_pct: str = Form("5"),
    max_pct_per_trade: str = Form("10"),
    daily_loss_limit_pct: str = Form("5"),
    stop_loss_pct: str = Form(""),
    take_profit_pct: str = Form(""),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        await auto.create_strategy(
            session,
            user,
            name=name,
            signal_rule_id=signal_rule_id,
            exchange_account_id=exchange_account_id,
            market_id=market_id,
            position_size_pct=tools_service.parse_decimal(position_size_pct, "размер позиции"),
            max_pct_per_trade=tools_service.parse_decimal(max_pct_per_trade, "максимум на сделку"),
            daily_loss_limit_pct=tools_service.parse_decimal(
                daily_loss_limit_pct, "дневной лимит убытка"
            ),
            stop_loss_pct=_optional(stop_loss_pct, "стоп-лосс"),
            take_profit_pct=_optional(take_profit_pct, "тейк-профит"),
        )
    except (auto.AutotradeError, tools_service.ToolsError) as exc:
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    await session.commit()
    flash.success(
        request,
        "Стратегия создана в режиме бумажных сделок и остановлена. "
        "Запустите её вручную, когда проверите настройки.",
    )
    return RedirectResponse(PAGE, status_code=303)


@router.get("/{strategy_id}", response_class=HTMLResponse)
async def strategy_page(
    request: Request,
    strategy_id: int,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        strategy = await auto.get_strategy(session, user, strategy_id)
    except auto.AutotradeError as exc:
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    market = await session.get(Market, strategy.market_id)

    return templates.TemplateResponse(
        request,
        "app/strategy.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "strategy": strategy,
            "symbol": market.symbol if market else "—",
            "risk": await auto.risk_state(session, strategy),
            "orders": await auto.recent_orders(session, strategy),
            "journal": await auto.recent_journal(session, strategy),
            "globally_enabled": settings.autotrade_enabled,
        },
    )


@router.post("/{strategy_id}/mode")
async def change_mode(
    request: Request,
    strategy_id: int,
    mode: str = Form(...),
    confirm: bool = Form(False),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)
    target = f"{PAGE}/{strategy_id}"

    try:
        strategy = await auto.get_strategy(session, user, strategy_id)
    except auto.AutotradeError as exc:
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    # Реальные сделки требуют отдельной отметки в форме: одного выбора
    # режима в списке для этого мало.
    if mode == "live" and not confirm:
        flash.error(
            request,
            "Чтобы включить реальные сделки, подтвердите согласие галочкой.",
        )
        return RedirectResponse(target, status_code=303)

    try:
        await auto.set_mode(session, user, strategy, mode)
    except auto.AutotradeError as exc:
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse(target, status_code=303)

    await session.commit()
    if mode == "live":
        flash.warn(
            request,
            "Режим реальных сделок включён. Стратегия остановлена — "
            "запустите её, когда будете готовы.",
        )
    else:
        flash.success(request, f"Режим изменён на «{mode}».")
    return RedirectResponse(target, status_code=303)


@router.post("/{strategy_id}/toggle")
async def toggle_strategy(
    request: Request,
    strategy_id: int,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)
    target = f"{PAGE}/{strategy_id}"

    try:
        strategy = await auto.get_strategy(session, user, strategy_id)
        await auto.set_active(session, strategy, not strategy.is_active)
    except auto.AutotradeError as exc:
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse(target, status_code=303)

    await session.commit()
    flash.success(request, "Стратегия запущена." if strategy.is_active else "Стратегия остановлена.")
    return RedirectResponse(target, status_code=303)


@router.post("/{strategy_id}/resume")
async def resume_after_halt(
    request: Request,
    strategy_id: int,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Снять дневную остановку вручную.

    Автоматически она не снимается: смысл лимита в том, чтобы человек
    посмотрел на происходящее, прежде чем продолжить.
    """
    auth.verify_csrf(request, csrf_token)
    target = f"{PAGE}/{strategy_id}"

    try:
        strategy = await auto.get_strategy(session, user, strategy_id)
    except auto.AutotradeError as exc:
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    state = await auto.risk_state(session, strategy)
    state.is_halted = False
    state.halted_reason = None
    await auto.journal(
        session, strategy, auto.EVENT_MODE, "Дневная остановка снята вручную."
    )
    await session.commit()

    flash.warn(request, "Остановка снята. Стратегию нужно запустить отдельно.")
    return RedirectResponse(target, status_code=303)


def _optional(raw: str, field: str) -> Decimal | None:
    return tools_service.parse_decimal(raw, field) if (raw or "").strip() else None
