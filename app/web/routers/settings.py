"""Настройки аккаунта: пароль и двухфакторная аутентификация."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import security, user_service
from app.web import auth, flash, qr
from app.web.templates_env import templates

router = APIRouter(prefix="/settings", tags=["settings"])

PAGE = "/settings/security"


@router.get("/security", response_class=HTMLResponse)
async def security_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "app/security.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "recovery_left": await user_service.unused_recovery_codes_count(session, user),
        },
    )


@router.post("/password")
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
        flash.error(request, "Новые пароли не совпадают.")
        return RedirectResponse(PAGE, status_code=303)

    try:
        await user_service.change_password(
            session, user, current_password=current_password, new_password=new_password
        )
    except user_service.UserServiceError as exc:
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    await session.commit()
    flash.success(request, "Пароль изменён.")
    return RedirectResponse(PAGE, status_code=303)


@router.post("/2fa/start", response_class=HTMLResponse)
async def start_totp(
    request: Request,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
):
    auth.verify_csrf(request, csrf_token)

    try:
        secret, uri = user_service.begin_totp_setup(user)
    except user_service.TotpAlreadyEnabled as exc:
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

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
        # Здесь отрисовываем страницу заново, а не перенаправляем: иначе
        # потеряется секрет, и пользователю пришлось бы сканировать новый
        # QR из-за одной опечатки в коде. Отката не делаем — сервис до
        # проверки кода ничего не записывает, а незакоммиченное всё равно
        # исчезнет при закрытии сессии.
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


@router.post("/2fa/disable")
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
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    await session.commit()
    flash.warn(request, "Двухфакторная аутентификация выключена.")
    return RedirectResponse(PAGE, status_code=303)
