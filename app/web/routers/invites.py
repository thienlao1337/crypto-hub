"""Приглашения. Раздел владельца: регистрация в панели закрытая."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Invite, User
from app.services import invite_service
from app.web import auth, flash
from app.web.templates_env import templates

router = APIRouter(prefix="/admin/invites", tags=["invites"])
settings = get_settings()


async def _context(request: Request, session: AsyncSession, user: User, **extra) -> dict:
    context = {
        "current_user": user,
        "csrf_token": auth.issue_csrf_token(request),
        "invites": await invite_service.list_invites(session),
        "public_url": settings.public_url.rstrip("/"),
    }
    context.update(extra)
    return context


@router.get("", response_class=HTMLResponse)
async def invites_page(
    request: Request,
    user: User = Depends(auth.require_owner),
    session: AsyncSession = Depends(get_session),
):
    return templates.TemplateResponse(
        request, "app/invites.html", await _context(request, session, user)
    )


@router.post("", response_class=HTMLResponse)
async def create_invite(
    request: Request,
    email: str = Form(""),
    note: str = Form(""),
    ttl_days: int = Form(14),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_owner),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    await invite_service.create_invite(
        session,
        created_by=user,
        email=email.strip() or None,
        note=note.strip() or None,
        # 0 в форме означает «без срока».
        ttl_days=ttl_days or None,
    )
    await session.commit()
    return RedirectResponse("/admin/invites", status_code=303)


@router.post("/{invite_id}/revoke", response_class=HTMLResponse)
async def revoke_invite(
    request: Request,
    invite_id: int,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_owner),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    invite = await session.get(Invite, invite_id)
    if invite is None:
        return RedirectResponse("/admin/invites", status_code=303)

    try:
        await invite_service.revoke_invite(session, invite, by=user)
    except invite_service.InviteError as exc:
        # Откат помечает загруженные объекты протухшими, поэтому страницу
        # не отрисовываем, а перенаправляем — см. app/web/flash.py.
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse("/admin/invites", status_code=303)

    await session.commit()
    flash.success(request, "Приглашение отозвано.")
    return RedirectResponse("/admin/invites", status_code=303)
