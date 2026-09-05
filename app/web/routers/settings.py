"""Настройки аккаунта: пароль и двухфакторная аутентификация."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import security, user_service
from app.web import auth, qr
from app.web.templates_env import templates

router = APIRouter(prefix="/settings", tags=["settings"])


async def _security_context(
    request: Request,
    session: AsyncSession,
    user: User,
    **extra,
) -> dict:
    context = {
        "current_user": user,
        "csrf_token": auth.issue_csrf_token(request),
        "recovery_left": await user_service.unused_recovery_codes_count(session, user),
    }
    context.update(extra)
    return context


@router.get("/security", response_class=HTMLResponse)
async def security_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "app/security.html",
        await _security_context(request, session, user),
    )


@router.post("/password", response_class=HTMLResponse)
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_repeat: str = Form(""),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    if new_password != new_password_repeat:
        return templates.TemplateResponse(
            request,
            "app/security.html",
            await _security_context(request, session, user, error="Новые пароли не совпадают."),
            status_code=400,
        )

    try:
        await user_service.change_password(
            session, user, current_password=current_password, new_password=new_password
        )
    except user_service.UserServiceError as exc:
        await session.rollback()
        return templates.TemplateResponse(
            request,
            "app/security.html",
            await _security_context(request, session, user, error=str(exc)),
            status_code=400,
        )

    await session.commit()
    return templates.TemplateResponse(
        request,
        "app/security.html",
        await _security_context(request, session, user, notice="Пароль изменён."),
    )


@router.post("/2fa/start", response_class=HTMLResponse)
async def start_totp(
    request: Request,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        secret, uri = user_service.begin_totp_setup(user)
    except user_service.TotpAlreadyEnabled as exc:
        return templates.TemplateResponse(
            request,
            "app/security.html",
            await _security_context(request, session, user, error=str(exc)),
            status_code=400,
        )

    # Секрет живёт в скрытом поле формы до подтверждения кодом: в сессию
    # его класть нельзя — cookie подписана, но не зашифрована.
    return templates.TemplateResponse(
        request,
        "app/totp_setup.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "secret": secret,
            "qr_data_uri": qr.data_uri(uri),
        },
    )


@router.post("/2fa/confirm", response_class=HTMLResponse)
async def confirm_totp(
    request: Request,
    secret: str = Form(...),
    code: str = Form(...),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        codes = await user_service.confirm_totp(session, user, secret=secret, code=code)
    except user_service.UserServiceError as exc:
        await session.rollback()
        return templates.TemplateResponse(
            request,
            "app/totp_setup.html",
            {
                "current_user": user,
                "csrf_token": auth.issue_csrf_token(request),
                "secret": secret,
                "qr_data_uri": qr.data_uri(
                    security.totp_provisioning_uri(secret, user.email, "Crypto Hub")
                ),
                "error": str(exc),
            },
            status_code=400,
        )

    await session.commit()
    return templates.TemplateResponse(
        request,
        "app/recovery_codes.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "codes": codes,
        },
    )


@router.post("/2fa/disable", response_class=HTMLResponse)
async def disable_totp(
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        await user_service.disable_totp(session, user, password=password)
    except user_service.UserServiceError as exc:
        await session.rollback()
        return templates.TemplateResponse(
            request,
            "app/security.html",
            await _security_context(request, session, user, error=str(exc)),
            status_code=400,
        )

    await session.commit()
    return RedirectResponse("/settings/security", status_code=303)
