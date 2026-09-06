"""Настройки аккаунта: пароль, двухфакторная аутентификация, уведомления."""

from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import notification_service, security, user_service, webpush
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
            "telegram_code": request.session.pop("telegram_code", None),
            "timezones": sorted(available_timezones()),
        },
    )


@router.post("/timezone")
async def change_timezone(
    request: Request,
    timezone_name: str = Form(...),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        await user_service.set_timezone(session, user, timezone_name)
    except user_service.UserServiceError as exc:
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    await session.commit()
    flash.success(request, f"Часовой пояс: {timezone_name}. Время на экранах пересчитано.")
    return RedirectResponse(PAGE, status_code=303)


@router.post("/theme")
async def change_theme(
    request: Request,
    theme: str = Form(...),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Запомнить выбранную тему за аккаунтом.

    Отвечает JSON, а не редиректом: переключатель темы срабатывает на
    любой странице, и уводить с неё из-за смены цвета незачем.
    """
    auth.verify_csrf(request, csrf_token)

    if theme not in ("dark", "light"):
        return JSONResponse({"error": "Неизвестная тема."}, status_code=400)

    user.theme = theme
    await session.commit()
    return {"ok": True}


@router.get("/notifications", response_class=HTMLResponse)
async def notification_settings_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "app/notification_settings.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "matrix": await notification_service.settings_matrix(session, user),
            "events": notification_service.EVENT_KINDS,
            "channels": notification_service.CHANNELS,
            "event_titles": notification_service.EVENT_TITLES,
            "event_hints": notification_service.EVENT_HINTS,
            "channel_titles": notification_service.CHANNEL_TITLES,
            "telegram_linked": user.telegram_id is not None,
            "push_enabled": webpush.is_configured(),
            "subscriptions": await webpush.list_subscriptions(session, user),
        },
    )


@router.post("/notifications")
async def save_notification_settings(
    request: Request,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Сохранить матрицу «событие × канал».

    Форма читается целиком, а не по одному изменённому флажку: снятая
    галочка ничего не отправляет, и разобрать её можно только по
    отсутствию в полном наборе.
    """
    auth.verify_csrf(request, csrf_token)
    form = await request.form()

    for event in notification_service.EVENT_KINDS:
        for channel in notification_service.CHANNELS:
            await notification_service.set_enabled(
                session,
                user.id,
                event,
                channel,
                f"{event}:{channel}" in form,
            )

    await session.commit()
    flash.success(request, "Настройки уведомлений сохранены.")
    return RedirectResponse("/settings/notifications", status_code=303)


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


@router.post("/telegram/link")
async def link_telegram(
    request: Request,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Выдать одноразовый код для привязки чата."""
    auth.verify_csrf(request, csrf_token)

    code = await user_service.issue_telegram_link_code(session, user)
    await session.commit()

    # Код нужен ровно на одну отрисовку, поэтому кладём его в сессию и
    # забираем при следующем показе страницы: в адресе ему не место.
    request.session["telegram_code"] = code
    return RedirectResponse(PAGE, status_code=303)


@router.post("/telegram/unlink")
async def unlink_telegram(
    request: Request,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    user.telegram_id = None
    user.telegram_username = None
    user.telegram_link_code = None
    user.telegram_link_expires_at = None
    await session.commit()

    flash.warn(request, "Telegram отвязан — уведомления в чат больше не придут.")
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
