"""Веб-пуш: выдача публичного ключа и подписка браузера.

Запросы сюда идут через fetch, а не через форму, поэтому тело —
JSON, а токен CSRF приезжает в нём же полем csrf_token.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import webpush
from app.web import auth
from app.web.templates_env import STATIC_DIR

logger = logging.getLogger(__name__)
router = APIRouter(tags=["push"])


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Service worker отдаётся с корня, а не из /static/.

    Область действия service worker'а ограничена каталогом, из которого
    он загружен: из /static/ он не смог бы открывать страницы панели по
    клику на уведомление.
    """
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        # Обновлённый воркер должен подхватываться, а не жить в кэше
        # неделю: браузер и так проверяет его при каждой регистрации.
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/push/config")
async def push_config(user: User = Depends(auth.require_user)):
    """Публичный ключ VAPID для подписки в браузере."""
    if not webpush.is_configured():
        return {"enabled": False}
    return {"enabled": True, "public_key": webpush.public_key()}


@router.post("/push/subscribe")
async def subscribe(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    if not webpush.is_configured():
        return JSONResponse({"error": "Веб-пуш на сервере не настроен."}, status_code=503)

    body = await _json(request)
    auth.verify_csrf(request, str(body.get("csrf_token") or ""))

    keys = body.get("keys") or {}
    try:
        await webpush.subscribe(
            session,
            user,
            endpoint=str(body.get("endpoint") or ""),
            p256dh=str(keys.get("p256dh") or ""),
            auth=str(keys.get("auth") or ""),
            label=auth.user_agent(request),
        )
    except webpush.WebPushError as exc:
        await session.rollback()
        return JSONResponse({"error": str(exc)}, status_code=400)

    await session.commit()
    return {"ok": True}


@router.post("/push/unsubscribe")
async def unsubscribe(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    body = await _json(request)
    auth.verify_csrf(request, str(body.get("csrf_token") or ""))

    removed = await webpush.unsubscribe(session, user, str(body.get("endpoint") or ""))
    await session.commit()
    return {"ok": removed}


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
