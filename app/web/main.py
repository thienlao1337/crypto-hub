"""Web panel entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings, verify_deployment
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
from app.web.routers import p2p as p2p_router
from app.web.routers import portfolio as portfolio_router
from app.web.routers import push as push_router
from app.web.routers import settings as settings_router
from app.web.routers import signals as signals_router
from app.web.routers import tools as tools_router
from app.web.templates_env import STATIC_DIR, templates

# Uvicorn only configures its own loggers, so warnings from our modules
# wouldn't go anywhere otherwise - and failures would have to be debugged
# blind.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the owner if the database is still empty.

    An error here isn't turned into a process crash: otherwise the container goes into a
    restart loop and the original cause gets lost in the logs. The problem shows up in
    /healthz and in the log entry below.
    """
    # Before anything else: example settings are an open door, and the app must
    # not start with them.
    verify_deployment(settings)

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
                "Created owner %s from SEED_OWNER_*. Change the password after the first login.",
                owner.email,
            )
    except Exception:
        logger.exception("Could not create the owner on startup")

    yield

    # Exchange subscriptions are held by one shared multiplexer - it must be
    # shut down on stop, otherwise connections hang until timeout.
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
app.include_router(p2p_router.router)
app.include_router(keys_router.router)
app.include_router(settings_router.router)
app.include_router(invites_router.router)
app.include_router(push_router.router)


# --- Common error handlers ---


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
    """One shared handler: the panel must never fail with a blank white screen."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
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
    """Liveness check of the process and the DB connection."""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "app": settings.app_name}
