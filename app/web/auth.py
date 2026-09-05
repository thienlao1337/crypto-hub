"""Сессия веб-панели: вход, CSRF, проверки доступа.

Сессия — подписанная cookie (Starlette SessionMiddleware). В ней лежит
только идентификатор пользователя: всё остальное читается из базы, чтобы
отключённый аккаунт терял доступ немедленно, а не после истечения cookie.
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
    """Нужен вход. Обработчик уводит на форму входа."""


class OwnerRequired(Exception):
    """Раздел доступен только владельцу панели."""


class CsrfInvalid(Exception):
    """Форма отправлена без корректного токена."""


# --- CSRF ---


def issue_csrf_token(request: Request) -> str:
    """Взять токен из сессии или завести новый.

    Токен привязан к сессии, поэтому чужая страница не может подставить
    свой: она не прочитает нашу cookie.
    """
    token = request.session.get(SESSION_CSRF)
    if not token:
        token = security.generate_token(24)
        request.session[SESSION_CSRF] = token
    return token


def verify_csrf(request: Request, token: str | None) -> None:
    expected = request.session.get(SESSION_CSRF)
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise CsrfInvalid("Форма устарела. Обновите страницу и попробуйте снова.")


# --- Состояние сессии ---


def start_session(request: Request, user: User) -> None:
    """Открыть сессию после успешного прохождения всех факторов.

    Сессия очищается целиком: перевыпуск идентификатора не даёт
    воспользоваться cookie, подсунутой до входа.
    """
    request.session.clear()
    request.session[SESSION_USER_ID] = user.id
    issue_csrf_token(request)


def set_pending_user(request: Request, user: User) -> None:
    """Пароль принят, ждём второй фактор."""
    request.session.pop(SESSION_USER_ID, None)
    request.session[SESSION_PENDING_USER_ID] = user.id
    issue_csrf_token(request)


def clear_session(request: Request) -> None:
    request.session.clear()


# --- Текущий пользователь ---


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User | None:
    user_id = request.session.get(SESSION_USER_ID)
    if not user_id:
        return None

    user = await user_service.get_by_id(session, user_id)
    if user is None or not user.is_active:
        # Аккаунт удалён или отключён, пока cookie ещё жива.
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


# --- Данные запроса для аудита ---


def client_ip(request: Request) -> str | None:
    """Адрес клиента с учётом обратного прокси.

    За Caddy или nginx настоящий адрес приходит в X-Forwarded-For.
    Заголовок подделывается, поэтому он годится для журнала, но не для
    ограничений — перебор паролей ограничивается по адресу почты.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")
