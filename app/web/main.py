"""Точка входа веб-панели."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import get_engine, session_scope
from app.exchanges.ws_hub import hub
from app.services import user_service
from app.web import auth
from app.web.routers import alerts as alerts_router
from app.web.routers import auth as auth_router
from app.web.routers import autotrade as autotrade_router
from app.web.routers import dashboard as dashboard_router
from app.web.routers import invites as invites_router
from app.web.routers import keys as keys_router
from app.web.routers import market as market_router
from app.web.routers import notifications as notifications_router
from app.web.routers import portfolio as portfolio_router
from app.web.routers import settings as settings_router
from app.web.routers import signals as signals_router
from app.web.routers import tools as tools_router
from app.web.templates_env import STATIC_DIR, templates

# Uvicorn настраивает только свои логгеры, поэтому предупреждения наших
# модулей иначе никуда не попадают — и разбирать сбой приходится вслепую.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Завести владельца, если база ещё пуста.

    Ошибку здесь не превращаем в падение процесса: контейнер иначе уходит
    в цикл перезапусков, и в логах теряется исходная причина. Проблему
    видно по /healthz и по записи ниже.
    """
    try:
        async with session_scope() as session:
            owner = await user_service.ensure_owner(
                session,
                email=settings.seed_owner_email,
                password=settings.seed_owner_password,
            )
            await session.commit()
        if owner is not None:
            logger.warning(
                "Создан владелец %s из SEED_OWNER_*. Смените пароль после первого входа.",
                owner.email,
            )
    except Exception:
        logger.exception("Не удалось создать владельца при старте")

    yield

    # Подписки на биржи держит один общий мультиплексор — при остановке
    # его надо погасить, иначе соединения зависают до таймаута.
    await hub.close()


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=settings.session_secure_cookie,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth_router.router)
app.include_router(dashboard_router.router)
app.include_router(portfolio_router.router)
app.include_router(market_router.router)
app.include_router(signals_router.router)
app.include_router(alerts_router.router)
app.include_router(notifications_router.router)
app.include_router(tools_router.router)
app.include_router(autotrade_router.router)
app.include_router(keys_router.router)
app.include_router(settings_router.router)
app.include_router(invites_router.router)


# --- Общие обработчики ошибок ---


@app.exception_handler(auth.LoginRequired)
async def handle_login_required(request: Request, exc: auth.LoginRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(auth.OwnerRequired)
async def handle_owner_required(request: Request, exc: auth.OwnerRequired):
    return templates.TemplateResponse(
        request,
        "error.html",
        {"title": "Нет доступа", "message": "Раздел доступен только владельцу панели."},
        status_code=403,
    )


@app.exception_handler(auth.CsrfInvalid)
async def handle_csrf_invalid(request: Request, exc: auth.CsrfInvalid):
    return templates.TemplateResponse(
        request,
        "error.html",
        {"title": "Форма устарела", "message": str(exc)},
        status_code=400,
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    """Один общий обработчик: панель не должна падать белым экраном."""
    logger.exception("Необработанная ошибка на %s %s", request.method, request.url.path)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "title": "Что-то сломалось",
            "message": "Ошибка записана в журнал. Попробуйте повторить действие.",
        },
        status_code=500,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Проверка живости процесса и соединения с БД."""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "app": settings.app_name}
