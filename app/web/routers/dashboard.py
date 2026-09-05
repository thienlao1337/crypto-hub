"""Главный экран: портфель, рынок, свежие события."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import dashboard_service, portfolio_service, signal_service
from app.web import auth
from app.web.templates_env import templates

router = APIRouter(tags=["dashboard"])


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    movers = await dashboard_service.top_movers(session)

    return templates.TemplateResponse(
        request,
        "app/dashboard.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "summary": await portfolio_service.build_summary(session, user),
            "stats": await dashboard_service.latest_global_stats(session),
            "gainers": movers["gainers"],
            "losers": movers["losers"],
            "events": await dashboard_service.recent_events(session, user),
            "signals": await signal_service.recent_signals(session, user_id=user.id, limit=5),
        },
    )
