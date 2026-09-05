"""Точка входа веб-панели.

Пока — только каркас и healthcheck. Экраны появятся на следующих этапах:
аутентификация и инвайты, затем портфель, рынок, сигналы и алерты.
"""

from fastapi import FastAPI
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import get_engine

settings = get_settings()

app = FastAPI(title=settings.app_name, debug=settings.debug)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=not settings.debug,
    same_site="lax",
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Проверка живости процесса и соединения с БД."""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "app": settings.app_name}
