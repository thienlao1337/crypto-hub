"""Web push: serving the public key and browser subscription.

Requests here come via fetch, not a form, so the body is JSON and the CSRF token arrives
in it as the csrf_token field.
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
    """The service worker is served from the root, not from /static/.

    A service worker's scope is limited to the directory it was loaded from: from
    /static/ it couldn't open panel pages when a notification is clicked.
    """
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        # An updated worker must be picked up, not live in the cache for a
        # week: the browser checks it on every registration anyway.
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/push/config")
async def push_config(user: User = Depends(auth.require_user)):
    """Public VAPID key for subscribing in the browser."""
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
