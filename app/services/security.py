"""Cryptographic primitives: secret encryption, passwords, TOTP.

The only place in the project where keys and hashes live. Everything else goes through
these functions and doesn't know the details.
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
    """ENCRYPTION_KEY is not set or is invalid."""


class DecryptionFailed(RuntimeError):
    """The value can't be decrypted with the current key."""


# --- Secret encryption (exchange API keys, TOTP secrets) ---


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
    """Encrypt a secret for storage in the database."""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a secret from the database.

    Fails if the key was changed: a database dump without ENCRYPTION_KEY is useless,
    which is the point - but such rows can't be recovered either.
    """
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise DecryptionFailed(
            "Значение не расшифровывается текущим ENCRYPTION_KEY."
        ) from exc


# --- Passwords ---


def _prepare_password(password: str) -> bytes:
    """Fold the password into a fixed 44 bytes before bcrypt.

    bcrypt truncates input at 72 bytes: without pre-hashing, a long passphrase silently
    loses its tail, and two different passwords with a common prefix become one. SHA-256
    + base64 removes that limit - the same trick the bcrypt_sha256 scheme uses.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare_password(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare_password(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # A broken or tampered hash in the database is no reason to crash the login form.
        return False


# --- Two-factor authentication (TOTP) ---


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str, issuer: str) -> str:
    """The string for the QR code understood by Google Authenticator and similar apps."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    """Verify a one-time code.

    valid_window=1 tolerates a clock drift of one step (±30 seconds) - without it users
    whose phone clock is off won't be able to log in.
    """
    if not code or not code.strip():
        return False
    try:
        return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)
    except (ValueError, TypeError):
        return False


# --- One-time codes and tokens ---


def generate_recovery_code() -> str:
    """Recovery code in readable form: 4f3a-9c21-be07."""
    raw = secrets.token_hex(6)
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def hash_recovery_code(code: str) -> str:
    """Hash a recovery code.

    SHA-256 rather than bcrypt here: we generate the code, it has 48 bits of randomness,
    and brute-forcing it from the hash is unrealistic. A slow hash is for passwords
    people make up, and checking ten codes through bcrypt would noticeably slow down
    login.
    """
    return hashlib.sha256(_normalize_code(code).encode("utf-8")).hexdigest()


def verify_recovery_code(code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_recovery_code(code), code_hash)


def _normalize_code(code: str) -> str:
    return code.strip().lower().replace(" ", "").replace("-", "")


def generate_token(length: int = 32) -> str:
    """Random token for invites and Telegram linking codes."""
    return secrets.token_urlsafe(length)


def generate_numeric_code(digits: int = 6) -> str:
    """A short numeric code - the user retypes it by hand."""
    return "".join(secrets.choice("0123456789") for _ in range(digits))
