"""Инструменты: конвертер и калькулятор сделки."""

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import tools_service
from app.web import auth
from app.web.templates_env import templates

router = APIRouter(prefix="/tools", tags=["tools"])


def _base_context(request: Request, user: User, **extra) -> dict:
    context = {
        "current_user": user,
        "csrf_token": auth.issue_csrf_token(request),
    }
    context.update(extra)
    return context


@router.get("", response_class=HTMLResponse)
async def tools_page(
    request: Request,
    user: User = Depends(auth.require_user),
):
    return templates.TemplateResponse(request, "app/tools.html", _base_context(request, user))


@router.post("/convert", response_class=HTMLResponse)
async def convert(
    request: Request,
    amount: str = Form(...),
    source: str = Form(...),
    target: str = Form(...),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    context = _base_context(
        request, user, convert_input={"amount": amount, "source": source, "target": target}
    )
    try:
        context["conversion"] = await tools_service.convert(
            session,
            amount=tools_service.parse_decimal(amount, "количество"),
            source=source,
            target=target,
        )
    except tools_service.ToolsError as exc:
        context["convert_error"] = str(exc)

    return templates.TemplateResponse(request, "app/tools.html", context)


@router.post("/trade", response_class=HTMLResponse)
async def trade(
    request: Request,
    side: str = Form("buy"),
    amount: str = Form(...),
    entry_price: str = Form(...),
    exit_price: str = Form(...),
    fee_pct: str = Form("0.1"),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
):
    auth.verify_csrf(request, csrf_token)

    context = _base_context(
        request,
        user,
        trade_input={
            "side": side,
            "amount": amount,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "fee_pct": fee_pct,
        },
    )
    try:
        context["trade"] = tools_service.calculate_trade(
            side=side,
            amount=tools_service.parse_decimal(amount, "количество"),
            entry_price=tools_service.parse_decimal(entry_price, "цена входа"),
            exit_price=tools_service.parse_decimal(exit_price, "цена выхода"),
            fee_pct=tools_service.parse_decimal(fee_pct, "комиссия"),
        )
    except tools_service.ToolsError as exc:
        context["trade_error"] = str(exc)

    return templates.TemplateResponse(request, "app/tools.html", context)
