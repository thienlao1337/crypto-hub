"""Лента уведомлений и счётчик непрочитанных."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import notification_service
from app.web import auth
from app.web.templates_env import templates

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    items = await notification_service.recent(session, user, limit=60)

    # Открыв ленту, пользователь их и прочитал — держать счётчик
    # ненулевым после этого бессмысленно.
    await notification_service.mark_all_read(session, user)
    await session.commit()

    return templates.TemplateResponse(
        request,
        "app/notifications.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "items": items,
        },
    )


@router.get("/count")
async def unread_count(
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Счётчик для значка в шапке."""
    return {"unread": await notification_service.unread_count(session, user)}


@router.post("/read")
async def mark_read(
    request: Request,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)
    await notification_service.mark_all_read(session, user)
    await session.commit()
    return RedirectResponse("/notifications", status_code=303)
