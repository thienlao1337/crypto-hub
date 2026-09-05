"""Вход, второй фактор, регистрация по приглашению."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import audit_service, invite_service, user_service
from app.web import auth
from app.web.templates_env import templates

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    user: User | None = Depends(auth.get_current_user),
):
    if user is not None:
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {"csrf_token": auth.issue_csrf_token(request)},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        user = await user_service.authenticate(
            session,
            email=email,
            password=password,
            ip=auth.client_ip(request),
            user_agent=auth.user_agent(request),
        )
    except user_service.UserServiceError as exc:
        # Записи о неудачной попытке сделал сервис — фиксируем их.
        await session.commit()
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {
                "csrf_token": auth.issue_csrf_token(request),
                "error": str(exc),
                "email": email,
            },
            status_code=401,
        )

    if user.totp_enabled:
        auth.set_pending_user(request, user)
        await session.commit()
        return RedirectResponse("/login/2fa", status_code=303)

    await user_service.complete_login(
        session,
        user,
        ip=auth.client_ip(request),
        user_agent=auth.user_agent(request),
    )
    await session.commit()
    auth.start_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.get("/login/2fa", response_class=HTMLResponse)
async def second_factor_form(
    request: Request,
    pending: User | None = Depends(auth.get_pending_user),
):
    if pending is None:
        return RedirectResponse("/login", status_code=303)

    return templates.TemplateResponse(
        request,
        "auth/login_2fa.html",
        {"csrf_token": auth.issue_csrf_token(request)},
    )


@router.post("/login/2fa", response_class=HTMLResponse)
async def second_factor_submit(
    request: Request,
    code: str = Form(...),
    csrf_token: str = Form(""),
    pending: User | None = Depends(auth.get_pending_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    if pending is None:
        return RedirectResponse("/login", status_code=303)

    if not await user_service.verify_second_factor(session, pending, code):
        await audit_service.log_login(
            session,
            email=pending.email,
            user_id=pending.id,
            is_success=False,
            failure_reason=audit_service.FAILURE_BAD_SECOND_FACTOR,
            ip=auth.client_ip(request),
            user_agent=auth.user_agent(request),
        )
        await session.commit()
        return templates.TemplateResponse(
            request,
            "auth/login_2fa.html",
            {
                "csrf_token": auth.issue_csrf_token(request),
                "error": "Код не подошёл.",
            },
            status_code=401,
        )

    await user_service.complete_login(
        session,
        pending,
        ip=auth.client_ip(request),
        user_agent=auth.user_agent(request),
    )
    await session.commit()
    auth.start_session(request, pending)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    # Токен проверяем, чтобы чужая страница не выкидывала пользователя.
    auth.verify_csrf(request, csrf_token)
    auth.clear_session(request)
    return RedirectResponse("/login", status_code=303)


@router.get("/register", response_class=HTMLResponse)
async def register_form(
    request: Request,
    code: str = "",
    session: AsyncSession = Depends(get_session),
):
    context: dict = {
        "csrf_token": auth.issue_csrf_token(request),
        "code": code,
        "min_password_length": user_service.MIN_PASSWORD_LENGTH,
    }

    if not code:
        context["error"] = "Нужен код приглашения — попросите его у владельца панели."
        return templates.TemplateResponse(request, "auth/register.html", context, status_code=400)

    try:
        invite = await invite_service.validate_code(session, code)
    except invite_service.InviteError as exc:
        context["error"] = str(exc)
        return templates.TemplateResponse(request, "auth/register.html", context, status_code=400)

    if invite.email:
        context["email"] = invite.email
        context["bound_email"] = True
    return templates.TemplateResponse(request, "auth/register.html", context)


@router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    code: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    password_repeat: str = Form(""),
    csrf_token: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    context: dict = {
        "csrf_token": auth.issue_csrf_token(request),
        "code": code,
        "email": email,
        "min_password_length": user_service.MIN_PASSWORD_LENGTH,
    }

    if password != password_repeat:
        context["error"] = "Пароли не совпадают."
        return templates.TemplateResponse(request, "auth/register.html", context, status_code=400)

    try:
        user = await invite_service.register_by_invite(
            session,
            code=code,
            email=email,
            password=password,
            ip=auth.client_ip(request),
            user_agent=auth.user_agent(request),
        )
    except (invite_service.InviteError, user_service.UserServiceError) as exc:
        await session.rollback()
        context["error"] = str(exc)
        return templates.TemplateResponse(request, "auth/register.html", context, status_code=400)

    await user_service.complete_login(
        session,
        user,
        ip=auth.client_ip(request),
        user_agent=auth.user_agent(request),
    )
    await session.commit()
    auth.start_session(request, user)
    return RedirectResponse("/", status_code=303)
