"""Web panel session: login, CSRF, access checks.

The session is a signed cookie (Starlette SessionMiddleware). It holds only the user id:
everything else is read from the database, so a disabled account loses access
immediately, not when the cookie expires.
"""

import hmac

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import security, user_service

SESSION_USER_ID = "user_id"
SESSION_PENDING_USER_ID = "pending_user_id"
SESSION_CSRF = "csrf_token"


class LoginRequired(Exception):
    """Login required. The handler redirects to the login form."""


class OwnerRequired(Exception):
    """This section is available only to the panel owner."""


class CsrfInvalid(Exception):
    """The form was submitted without a valid token."""


# --- CSRF ---


def issue_csrf_token(request: Request) -> str:
    """Take the token from the session or create a new one.

    The token is bound to the session, so a third-party page can't substitute its own:
    it can't read our cookie.
    """
    token = request.session.get(SESSION_CSRF)
    if not token:
        token = security.generate_token(24)
        request.session[SESSION_CSRF] = token
    return token


def verify_csrf(request: Request, token: str | None) -> None:
    expected = request.session.get(SESSION_CSRF)
    if not expected or not token:
        raise CsrfInvalid("Форма устарела. Обновите страницу и попробуйте снова.")

    # Compare bytes, not strings: compare_digest on strings with non-ASCII
    # characters raises TypeError, and Cyrillic sent in the token field would
    # crash the handler instead of a clean rejection.
    if not hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8")):
        raise CsrfInvalid("Форма устарела. Обновите страницу и попробуйте снова.")


# --- Session state ---


def start_session(request: Request, user: User) -> None:
    """Open a session after all factors have been passed.

    The session is cleared entirely: reissuing the identifier prevents using a cookie
    planted before login.
    """
    request.session.clear()
    request.session[SESSION_USER_ID] = user.id
    issue_csrf_token(request)


def set_pending_user(request: Request, user: User) -> None:
    """Password accepted, waiting for the second factor."""
    request.session.pop(SESSION_USER_ID, None)
    request.session[SESSION_PENDING_USER_ID] = user.id
    issue_csrf_token(request)


def clear_session(request: Request) -> None:
    request.session.clear()


# --- Current user ---


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User | None:
    user_id = request.session.get(SESSION_USER_ID)
    if not user_id:
        return None

    user = await user_service.get_by_id(session, user_id)
    if user is None or not user.is_active:
        # The account was deleted or disabled while the cookie is still alive.
        request.session.clear()
        return None
    return user


async def get_pending_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User | None:
    user_id = request.session.get(SESSION_PENDING_USER_ID)
    if not user_id:
        return None

    user = await user_service.get_by_id(session, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


async def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


async def require_owner(user: User = Depends(require_user)) -> User:
    if not user.is_owner:
        raise OwnerRequired()
    return user


# --- Request data for the audit log ---


def client_ip(request: Request) -> str | None:
    """Client address, taking the reverse proxy into account.

    Behind Caddy or nginx the real address arrives in X-Forwarded-For. The header can be
    forged, so it's fine for the log but not for limits - password brute-forcing is
    limited per email address.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")
