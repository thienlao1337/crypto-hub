"""Подключение бирж: добавление, проверка и удаление API-ключей."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import exchange_keys_service as keys_service
from app.services import p2p_service
from app.web import auth, flash
from app.web.templates_env import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings/keys", tags=["keys"])

PAGE = "/settings/keys"


@router.get("", response_class=HTMLResponse)
async def keys_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    accounts = await keys_service.list_accounts(session, user)
    exchanges = await keys_service.list_exchanges(session)
    by_id = {exchange.id: exchange for exchange in exchanges}

    return templates.TemplateResponse(
        request,
        "app/keys.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "exchanges": exchanges,
            "accounts": [(a, by_id.get(a.exchange_id)) for a in accounts],
        },
    )


@router.post("")
async def add_key(
    request: Request,
    exchange_code: str = Form(...),
    api_key: str = Form(...),
    api_secret: str = Form(...),
    label: str = Form("main"),
    testnet: bool = Form(False),
    want_trading: bool = Form(False),
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        account = await keys_service.add_account(
            session,
            user,
            exchange_code=exchange_code,
            api_key=api_key,
            api_secret=api_secret,
            label=label,
            testnet=testnet,
            want_trading=want_trading,
        )
    except keys_service.ExchangeKeyError as exc:
        # После отката все загруженные объекты протухают, и обращение к
        # любому их полю тянет SELECT из синхронного кода. Поэтому здесь
        # не отрисовываем страницу, а перенаправляем: следующий запрос
        # начнётся с чистой сессией.
        await session.rollback()
        flash.error(request, str(exc))
        return RedirectResponse(PAGE, status_code=303)

    if account.requested_trading and not account.allow_trading:
        flash.warn(
            request,
            "Ключ подключён в режиме чтения: биржа не подтвердила право на "
            "торговлю. Проверьте разрешения ключа в кабинете биржи.",
        )
    else:
        flash.success(request, "Ключ подключён.")

    await session.commit()
    return RedirectResponse(PAGE, status_code=303)


@router.post("/{account_id}/p2p")
async def request_p2p(
    request: Request,
    account_id: int,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Запросить у площадки доступ к P2P для этого ключа.

    Отдельным действием, а не галочкой при добавлении: статус
    рекламодателя или мерчанта оформляется на площадке и появляется
    позже, когда ключ уже подключён.
    """
    auth.verify_csrf(request, csrf_token)

    account = await keys_service.get_account(session, user, account_id)
    if account is None:
        flash.error(request, "Подключение не найдено.")
        return RedirectResponse(PAGE, status_code=303)

    account.requested_p2p = True
    adapter = await p2p_service.build_adapter(session, account)
    try:
        allowed = await p2p_service.verify_access(session, account, adapter)
    except Exception as exc:
        await session.rollback()
        logger.warning("Проверка доступа к P2P для %s не удалась: %s", account_id, exc)
        flash.error(request, "Не удалось проверить доступ к P2P. Попробуйте позже.")
        return RedirectResponse(PAGE, status_code=303)
    finally:
        await adapter.close()

    await session.commit()

    if allowed:
        flash.success(request, "Площадка подтвердила доступ к P2P.")
    else:
        flash.warn(
            request,
            "Площадка не открыла доступ к P2P. Нужен статус рекламодателя "
            "(Bybit) или верифицированного мерчанта (Binance) — он "
            "оформляется в кабинете площадки, не здесь.",
        )
    return RedirectResponse(PAGE, status_code=303)


@router.post("/{account_id}/recheck")
async def recheck_key(
    request: Request,
    account_id: int,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        account = await keys_service.get_account(session, user, account_id)
    except keys_service.AccountNotFound:
        return RedirectResponse(PAGE, status_code=303)

    check = await keys_service.recheck_account(session, account)
    await session.commit()

    if check.is_valid:
        flash.success(request, "Ключ действителен.")
    else:
        flash.error(request, check.error or "Биржа отклонила ключ.")
    return RedirectResponse(PAGE, status_code=303)


@router.post("/{account_id}/delete")
async def delete_key(
    request: Request,
    account_id: int,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    auth.verify_csrf(request, csrf_token)

    try:
        account = await keys_service.get_account(session, user, account_id)
    except keys_service.AccountNotFound:
        return RedirectResponse(PAGE, status_code=303)

    await keys_service.delete_account(session, user, account)
    await session.commit()
    flash.success(request, "Подключение удалено.")
    return RedirectResponse(PAGE, status_code=303)
