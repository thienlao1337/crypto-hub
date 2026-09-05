"""Алерты: список, создание, удаление."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import AlertType, Exchange, Market, MarketTicker, User
from app.models.market import MARKET_TYPE_SPOT
from app.services import alert_service, watchlist_service
from app.web import auth, flash
from app.web.templates_env import templates

router = APIRouter(prefix="/alerts", tags=["alerts"])

PAGE = "/alerts"


@router.get("", response_class=HTMLResponse)
async def alerts_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    types = (
        await session.execute(
            select(AlertType).where(AlertType.is_active.is_(True)).order_by(AlertType.sort_order)
        )
    ).scalars().all()

    # Предлагаем пары из списка отслеживания, а не всю тысячу: алерт на
    # пару, за которой никто не следит, всё равно некому проверять —
    # фоновый процесс качает свечи только по watchlist.
    watched = await watchlist_service.list_items(session, user)
    if not watched:
        markets = (
            await session.execute(
                select(Market.id, Market.symbol, Exchange.code)
                .join(Exchange, Exchange.id == Market.exchange_id)
                .join(MarketTicker, MarketTicker.market_id == Market.id)
                .where(Market.market_type == MARKET_TYPE_SPOT)
                .order_by(MarketTicker.quote_volume_24h.desc().nulls_last())
                .limit(20)
            )
        ).all()
        options = [{"id": mid, "symbol": symbol, "exchange": code} for mid, symbol, code in markets]
    else:
        options = [
            {"id": row["item"].market_id, "symbol": row["symbol"], "exchange": row["exchange"]}
            for row in watched
        ]

    return templates.TemplateResponse(
        request,
        "app/alerts.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "alerts": await alert_service.list_alerts(session, user),
            "alert_types": types,
            "markets": options,
            "has_watchlist": bool(watched),
        },
    )


@router.post("")
async def create_alert(
    request: Request,
    market_id: int = Form(...),
    type_code: str = Form(...),
    level: str = Form(""),
    pct: str = Form(""),
    window_minutes: int = Form(60),
    threshold: str = Form(""),
    period: int = Form(14),
    direction: str = Form("above"),
    cooldown_minutes: int = Form(60),
    notify_web: bool = Form(True),
    notify_telegram: bool = Form(True),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    params = _params_for(type_code, level, pct, window_minutes, threshold, period, direction)

    try:
        await alert_service.create_alert(
            session,
            user,
            market_id=market_id,
            type_code=type_code,
            params=params,
            cooldown_seconds=max(1, cooldown_minutes) * 60,
            notify_web=notify_web,
            notify_telegram=notify_telegram,
        )
    except alert_service.AlertError as exc:
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    await session.commit()
    flash.success(request, "Алерт создан.")
    return RedirectResponse(PAGE, status_code=303)


@router.post("/{alert_id}/toggle")
async def toggle_alert(
    request: Request,
    alert_id: int,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        alert = await alert_service.get_alert(session, user, alert_id)
    except alert_service.AlertError as exc:
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    alert.is_active = not alert.is_active
    await session.commit()
    return RedirectResponse(PAGE, status_code=303)


@router.post("/{alert_id}/delete")
async def delete_alert(
    request: Request,
    alert_id: int,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        alert = await alert_service.get_alert(session, user, alert_id)
    except alert_service.AlertError as exc:
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    await session.delete(alert)
    await session.commit()
    flash.success(request, "Алерт удалён.")
    return RedirectResponse(PAGE, status_code=303)


def _params_for(
    type_code: str,
    level: str,
    pct: str,
    window_minutes: int,
    threshold: str,
    period: int,
    direction: str,
) -> dict:
    """Собрать параметры под конкретный тип, отбросив чужие поля."""
    if type_code in (alert_service.TYPE_PRICE_ABOVE, alert_service.TYPE_PRICE_BELOW):
        return {"level": level.strip().replace(",", ".")}
    if type_code == alert_service.TYPE_PCT_CHANGE:
        return {"pct": pct.strip().replace(",", "."), "window_minutes": window_minutes}
    if type_code == alert_service.TYPE_RSI:
        return {
            "threshold": threshold.strip().replace(",", "."),
            "period": period,
            "direction": direction,
        }
    return {}
