"""Алерты: список, создание, правка, удаление."""

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

DEFAULT_VALUES = {
    "market_id": None,
    "type_code": None,
    "level": "",
    "pct": "",
    "window_minutes": 60,
    "threshold": "",
    "period": 14,
    "direction": "above",
    "cooldown_minutes": 60,
    "notify_web": True,
    "notify_telegram": True,
}


@router.get("", response_class=HTMLResponse)
async def alerts_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    markets, has_watchlist = await _market_options(session, user)

    return templates.TemplateResponse(
        request,
        "app/alerts.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "alerts": await alert_service.list_alerts(session, user),
            "alert_types": await _alert_types(session),
            "markets": markets,
            "has_watchlist": has_watchlist,
            "values": dict(DEFAULT_VALUES),
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
    notify_web: bool = Form(False),
    notify_telegram: bool = Form(False),
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


@router.get("/{alert_id}/edit", response_class=HTMLResponse)
async def edit_alert_page(
    request: Request,
    alert_id: int,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        alert = await alert_service.get_alert(session, user, alert_id)
    except alert_service.AlertError as exc:
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    markets, _ = await _market_options(session, user)
    alert_type = await session.get(AlertType, alert.alert_type_id)

    # Пара алерта может не входить в список отслеживания — например,
    # её убрали оттуда позже. В выборе она должна остаться, иначе
    # сохранение формы молча переставило бы алерт на другую пару.
    if not any(option["id"] == alert.market_id for option in markets):
        market = await session.get(Market, alert.market_id)
        if market is not None:
            exchange = await session.get(Exchange, market.exchange_id)
            markets.insert(
                0,
                {
                    "id": market.id,
                    "symbol": market.symbol,
                    "exchange": exchange.code if exchange else "?",
                },
            )

    return templates.TemplateResponse(
        request,
        "app/alert_edit.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "alert": alert,
            "alert_types": await _alert_types(session),
            "markets": markets,
            "values": _values_from(alert, alert_type.code if alert_type else None),
            "triggers": await alert_service.recent_triggers(session, alert),
        },
    )


@router.post("/{alert_id}")
async def update_alert(
    request: Request,
    alert_id: int,
    market_id: int = Form(...),
    type_code: str = Form(...),
    level: str = Form(""),
    pct: str = Form(""),
    window_minutes: int = Form(60),
    threshold: str = Form(""),
    period: int = Form(14),
    direction: str = Form("above"),
    cooldown_minutes: int = Form(60),
    notify_web: bool = Form(False),
    notify_telegram: bool = Form(False),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)
    target = f"{PAGE}/{alert_id}/edit"

    try:
        alert = await alert_service.get_alert(session, user, alert_id)
    except alert_service.AlertError as exc:
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    params = _params_for(type_code, level, pct, window_minutes, threshold, period, direction)

    try:
        await alert_service.update_alert(
            session,
            alert,
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
        return RedirectResponse(target, status_code=303)

    await session.commit()
    flash.success(request, "Алерт изменён.")
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


async def _alert_types(session: AsyncSession) -> list[AlertType]:
    result = await session.execute(
        select(AlertType).where(AlertType.is_active.is_(True)).order_by(AlertType.sort_order)
    )
    return list(result.scalars())


async def _market_options(
    session: AsyncSession, user: User
) -> tuple[list[dict], bool]:
    """Пары для выпадающего списка.

    Предлагаем список отслеживания, а не всю тысячу пар: алерт по паре,
    за которой никто не следит, некому проверять — фоновый процесс
    качает свечи только по watchlist.
    """
    watched = await watchlist_service.list_items(session, user)
    if watched:
        options = [
            {"id": row["item"].market_id, "symbol": row["symbol"], "exchange": row["exchange"]}
            for row in watched
        ]
        return options, True

    rows = await session.execute(
        select(Market.id, Market.symbol, Exchange.code)
        .join(Exchange, Exchange.id == Market.exchange_id)
        .join(MarketTicker, MarketTicker.market_id == Market.id)
        .where(Market.market_type == MARKET_TYPE_SPOT)
        .order_by(MarketTicker.quote_volume_24h.desc().nulls_last())
        .limit(20)
    )
    return [
        {"id": market_id, "symbol": symbol, "exchange": code}
        for market_id, symbol, code in rows
    ], False


def _values_from(alert, type_code: str | None) -> dict:
    """Заполнить форму значениями существующего алерта."""
    params = alert.params or {}
    values = dict(DEFAULT_VALUES)
    values.update(
        {
            "market_id": alert.market_id,
            "type_code": type_code,
            "level": params.get("level", ""),
            "pct": params.get("pct", ""),
            "window_minutes": params.get("window_minutes", 60),
            "threshold": params.get("threshold", ""),
            "period": params.get("period", 14),
            "direction": params.get("direction", "above"),
            "cooldown_minutes": max(1, alert.cooldown_seconds // 60),
            "notify_web": alert.notify_web,
            "notify_telegram": alert.notify_telegram,
        }
    )
    return values


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
