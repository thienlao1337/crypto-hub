"""Настройки из примера не должны доезжать до прода.

Все они рабочие: с ними панель открывается и ничего не жалуется. Именно
поэтому проверка и нужна — молчаливая дыра хуже громкого отказа.
"""

import pytest

from app.config import (
    DEFAULT_OWNER_PASSWORD,
    DEFAULT_POSTGRES_PASSWORD,
    DEFAULT_SESSION_SECRET,
    Settings,
    deployment_problems,
    verify_deployment,
)

GOOD = {
    "session_secret": "P6WQ0-настоящий-секрет-длиной-побольше",
    "encryption_key": "Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A=",
    "seed_owner_password": "не-из-примера",
    "postgres_password": "не-из-примера",
    "debug": False,
}


def settings(**overrides) -> Settings:
    # _env_file=None: настройки разработчика не должны влиять на проверку.
    return Settings(_env_file=None, **{**GOOD, **overrides})


def test_good_settings_pass():
    blocking, warnings = deployment_problems(settings())

    assert blocking == []
    assert warnings == []
    verify_deployment(settings())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_secret", DEFAULT_SESSION_SECRET),
        ("encryption_key", ""),
        ("seed_owner_password", DEFAULT_OWNER_PASSWORD),
    ],
)
def test_example_values_block_startup(field, value):
    config = settings(**{field: value})

    blocking, _ = deployment_problems(config)
    assert blocking, f"{field} из примера должен останавливать запуск"

    with pytest.raises(RuntimeError):
        verify_deployment(config)


def test_message_says_what_to_do():
    """Отказ без инструкции — это просто сломанный деплой."""
    with pytest.raises(RuntimeError) as info:
        verify_deployment(settings(session_secret=DEFAULT_SESSION_SECRET))

    assert "SESSION_SECRET" in str(info.value)
    assert "token_urlsafe" in str(info.value)


def test_debug_only_warns():
    """Разработчику незачем каждый раз заводить настоящие секреты."""
    config = settings(session_secret=DEFAULT_SESSION_SECRET, debug=True)

    verify_deployment(config)


def test_database_password_only_warns():
    """Наружу база не публикуется — это замечание, а не блокировка."""
    blocking, warnings = deployment_problems(
        settings(postgres_password=DEFAULT_POSTGRES_PASSWORD)
    )

    assert blocking == []
    assert any("POSTGRES_PASSWORD" in item for item in warnings)


def test_insecure_cookie_on_public_address_warns():
    blocking, warnings = deployment_problems(
        settings(session_secure_cookie=False, public_url="https://hub.example.com")
    )

    assert blocking == []
    assert any("SESSION_SECURE_COOKIE" in item for item in warnings)


def test_insecure_cookie_on_localhost_is_fine():
    """Локальный запуск по http — обычный сценарий разработки."""
    _, warnings = deployment_problems(
        settings(session_secure_cookie=False, public_url="http://localhost:8000")
    )

    assert warnings == []
