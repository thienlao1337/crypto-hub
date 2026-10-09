"""Every state-changing route must check the CSRF token.

The check is static - over the routers' source code - and that's deliberate. One could
write a regular test for every POST, but a route added tomorrow simply won't get such a
test, and the hole will slip through unnoticed. That's exactly what happened with
/portfolio/sync: the token was in the function signature, but the check was never
called, and it didn't stand out when reading the code.

Formally this tests discipline, not behaviour. The cost of a mistake here is actions on
behalf of a logged-in user triggered by a link from another site.
"""

import io
import re
from pathlib import Path

import pytest

ROUTERS_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "routers"
MUTATING = ("post(", "put(", "patch(", "delete(")

# Routes that don't need the check - with a justification. An empty list is
# better than a list without explanations: an exception without a reason
# eventually stops being an exception.
EXEMPT: dict[str, str] = {}


def mutating_routes() -> list[tuple[str, str, str]]:
    """All state-changing routes: (file, function name, body)."""
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
            # The body runs until the next route decorator.
            body = block.split("\n@router.")[0]
            found.append((path.name, name.group(1), body))
    return found


def test_routes_were_found():
    """If parsing breaks, the test below would turn green and useless."""
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
    """A state-changing route must require login.

    The exceptions are login, registration and logout: they exist precisely for those
    who haven't logged in yet or already logged out.
    """
    if handler in ("login_submit", "register_submit", "second_factor_submit", "logout"):
        pytest.skip("маршруты самой аутентификации")

    assert "require_user" in body or "require_owner" in body, (
        f"{module}::{handler} доступен без входа"
    )
