import base64
import os

import pytest

from app.services import security

# Ключ шифрования подставляет автоиспользуемая фикстура из conftest.


# --- Шифрование ---


def test_encrypt_decrypt_roundtrip():
    secret = "bybit-api-key-12345"
    token = security.encrypt_secret(secret)

    assert token != secret
    assert security.decrypt_secret(token) == secret


def test_encryption_is_not_deterministic():
    """Два шифрования одного значения дают разные токены.

    Иначе по базе было бы видно, что у двух пользователей одинаковый ключ.
    """
    assert security.encrypt_secret("same") != security.encrypt_secret("same")


def test_decrypt_with_other_key_fails(monkeypatch):
    token = security.encrypt_secret("secret")

    other = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(security.get_settings(), "encryption_key", other, raising=False)
    security._fernet.cache_clear()

    with pytest.raises(security.DecryptionFailed):
        security.decrypt_secret(token)


def test_missing_key_raises_clear_error(monkeypatch):
    monkeypatch.setattr(security.get_settings(), "encryption_key", "", raising=False)
    security._fernet.cache_clear()

    with pytest.raises(security.EncryptionNotConfigured):
        security.encrypt_secret("anything")


# --- Пароли ---


def test_password_roundtrip():
    hashed = security.hash_password("correct horse battery staple")

    assert security.verify_password("correct horse battery staple", hashed)
    assert not security.verify_password("wrong password", hashed)


def test_password_salt_differs_per_call():
    assert security.hash_password("same") != security.hash_password("same")


def test_long_passphrases_are_not_truncated():
    """Пароли длиннее 72 байт должны различаться.

    Голый bcrypt обрезал бы их до общего префикса и признал одинаковыми.
    """
    base = "a" * 80
    hashed = security.hash_password(base + "TAIL-ONE")

    assert security.verify_password(base + "TAIL-ONE", hashed)
    assert not security.verify_password(base + "TAIL-TWO", hashed)


def test_broken_hash_does_not_raise():
    assert not security.verify_password("whatever", "не-хеш-вовсе")


# --- TOTP ---


def test_totp_accepts_current_code():
    import pyotp

    secret = security.generate_totp_secret()
    code = pyotp.TOTP(secret).now()

    assert security.verify_totp(secret, code)
    assert security.verify_totp(secret, f" {code} ")


def test_totp_rejects_garbage():
    secret = security.generate_totp_secret()

    assert not security.verify_totp(secret, "000000")
    assert not security.verify_totp(secret, "")
    assert not security.verify_totp(secret, "не код")


def test_provisioning_uri_contains_issuer_and_account():
    secret = security.generate_totp_secret()
    uri = security.totp_provisioning_uri(secret, "owner@example.com", "Crypto Hub")

    assert uri.startswith("otpauth://totp/")
    assert "Crypto%20Hub" in uri
    assert secret in uri


# --- Коды восстановления ---


def test_recovery_code_roundtrip():
    code = security.generate_recovery_code()
    code_hash = security.hash_recovery_code(code)

    assert security.verify_recovery_code(code, code_hash)
    assert not security.verify_recovery_code(security.generate_recovery_code(), code_hash)


def test_recovery_code_ignores_formatting():
    """Пользователь перепечатывает код руками — регистр и дефисы не важны."""
    code = security.generate_recovery_code()
    code_hash = security.hash_recovery_code(code)

    assert security.verify_recovery_code(code.upper(), code_hash)
    assert security.verify_recovery_code(code.replace("-", ""), code_hash)
    assert security.verify_recovery_code(f"  {code}  ", code_hash)


def test_generated_codes_are_unique():
    codes = {security.generate_recovery_code() for _ in range(200)}
    assert len(codes) == 200
