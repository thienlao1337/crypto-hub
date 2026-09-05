"""Криптографические примитивы: шифрование секретов, пароли, TOTP.

Единственное место в проекте, где живут ключи и хеши. Всё остальное
работает через эти функции и не знает деталей.
"""

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache

import bcrypt
import pyotp
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class EncryptionNotConfigured(RuntimeError):
    """ENCRYPTION_KEY не задан или задан некорректно."""


class DecryptionFailed(RuntimeError):
    """Значение не расшифровывается текущим ключом."""


# --- Шифрование секретов (API-ключи бирж, TOTP-секреты) ---


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise EncryptionNotConfigured(
            "ENCRYPTION_KEY не задан. Сгенерировать: python -c "
            '"from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise EncryptionNotConfigured(
            "ENCRYPTION_KEY некорректен: нужен urlsafe base64 из 32 байт."
        ) from exc


def encrypt_secret(value: str) -> str:
    """Зашифровать секрет для хранения в базе."""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Расшифровать секрет из базы.

    Падает, если ключ сменили: дамп базы без ENCRYPTION_KEY бесполезен,
    в этом и смысл — но и восстановить такие записи нельзя.
    """
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise DecryptionFailed(
            "Значение не расшифровывается текущим ENCRYPTION_KEY."
        ) from exc


# --- Пароли ---


def _prepare_password(password: str) -> bytes:
    """Свернуть пароль в фиксированные 44 байта перед bcrypt.

    bcrypt обрезает вход на 72 байтах: без предварительного хеширования
    длинная парольная фраза молча теряет хвост, и два разных пароля с
    общим началом становятся одним. SHA-256 + base64 снимает это
    ограничение — тот же приём использует схема bcrypt_sha256.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare_password(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare_password(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # Битый или подменённый хеш в базе — не повод ронять форму входа.
        return False


# --- Двухфакторная аутентификация (TOTP) ---


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str, issuer: str) -> str:
    """Строка для QR-кода, которую понимают Google Authenticator и аналоги."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    """Проверить одноразовый код.

    valid_window=1 допускает расхождение часов на один шаг (±30 секунд) —
    без этого пользователи с неточным временем на телефоне не войдут.
    """
    if not code or not code.strip():
        return False
    try:
        return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)
    except (ValueError, TypeError):
        return False


# --- Одноразовые коды и токены ---


def generate_recovery_code() -> str:
    """Код восстановления в читаемом виде: 4f3a-9c21-be07."""
    raw = secrets.token_hex(6)
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def hash_recovery_code(code: str) -> str:
    """Хешировать код восстановления.

    Здесь SHA-256, а не bcrypt: код генерируем мы, в нём 48 бит
    случайности, подбор по хешу нереален. Медленный хеш нужен паролям,
    которые придумывает человек, а проверка десяти кодов через bcrypt
    заметно тормозила бы вход.
    """
    return hashlib.sha256(_normalize_code(code).encode("utf-8")).hexdigest()


def verify_recovery_code(code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_recovery_code(code), code_hash)


def _normalize_code(code: str) -> str:
    return code.strip().lower().replace(" ", "").replace("-", "")


def generate_token(length: int = 32) -> str:
    """Случайный токен для инвайтов и кодов привязки Telegram."""
    return secrets.token_urlsafe(length)


def generate_numeric_code(digits: int = 6) -> str:
    """Короткий числовой код — его пользователь перепечатывает вручную."""
    return "".join(secrets.choice("0123456789") for _ in range(digits))
