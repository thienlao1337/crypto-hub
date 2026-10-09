"""Signals: feed, justification and accuracy statistics."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.db import get_session
from app.models import User
from app.services import signal_service
from app.web import auth
from app.web.templates_env import templates
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("", response_class=HTMLResponse)
async def signals_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await signal_service.recent_signals(session, user_id=user.id, limit=60)
    stats = await signal_service.accuracy(session)

    return templates.TemplateResponse(
        request,
        "app/signals.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "rows": rows,
            "stats": stats,
        },
    )
