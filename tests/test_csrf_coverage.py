"""Каждый изменяющий маршрут обязан проверять токен CSRF.

Проверка статическая — по исходникам роутеров, — и это осознанно. Написать
по обычному тесту на каждый POST можно, но появившийся завтра маршрут
такого теста просто не получит, и дыра проедет незамеченной. Ровно так и
случилось с /portfolio/sync: токен в подписи функции был, а вызова
проверки не было, и по виду кода это не бросалось в глаза.

Формально это тест не поведения, а дисциплины. Цена ошибки здесь —
действия от имени вошедшего пользователя по ссылке с чужого сайта.
"""

import io
import re
from pathlib import Path

import pytest

ROUTERS_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "routers"
MUTATING = ("post(", "put(", "patch(", "delete(")

# Маршруты, которым проверка не нужна, — с обоснованием. Пустой список
# лучше, чем список без объяснений: исключение без причины со временем
# перестаёт быть исключением.
EXEMPT: dict[str, str] = {}


def mutating_routes() -> list[tuple[str, str, str]]:
    """Все изменяющие маршруты: (файл, имя функции, тело)."""
    found = []
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        source = io.open(path, encoding="utf-8").read()
        blocks = re.split(r"\n@router\.", source)
        for block in blocks[1:]:
            head = block.split("\n", 1)[0]
            if not head.startswith(MUTATING):
                continue
            name = re.search(r"async def (\w+)", block)
            if name is None:
                continue
            # Тело — до следующего декоратора маршрута.
            body = block.split("\n@router.")[0]
            found.append((path.name, name.group(1), body))
    return found


def test_routes_were_found():
    """Если разбор сломается, тест ниже станет зелёным и бесполезным."""
    routes = mutating_routes()

    assert len(routes) > 20, f"нашлось всего {len(routes)} маршрутов — разбор сломан"


@pytest.mark.parametrize(
    ("module", "handler", "body"),
    [pytest.param(*route, id=f"{route[0]}::{route[1]}") for route in mutating_routes()],
)
def test_mutating_route_verifies_csrf(module, handler, body):
    if handler in EXEMPT:
        pytest.skip(EXEMPT[handler])

    assert "verify_csrf" in body, (
        f"{module}::{handler} меняет состояние, но не проверяет токен CSRF"
    )


@pytest.mark.parametrize(
    ("module", "handler", "body"),
    [pytest.param(*route, id=f"{route[0]}::{route[1]}") for route in mutating_routes()],
)
def test_mutating_route_requires_user(module, handler, body):
    """Изменяющий маршрут должен требовать вход.

    Исключение — вход, регистрация и выход: они и существуют для тех, кто
    ещё или уже не вошёл.
    """
    if handler in ("login_submit", "register_submit", "second_factor_submit", "logout"):
        pytest.skip("маршруты самой аутентификации")

    assert "require_user" in body or "require_owner" in body, (
        f"{module}::{handler} доступен без входа"
    )
