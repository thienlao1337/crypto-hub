"""Сообщения между запросами.

Нужны для схемы «отправил — перенаправил — показал»: обработчик формы
после ошибки не отрисовывает страницу сам, а кладёт сообщение сюда и
отправляет браузер на GET.

Так решается конкретная проблема: session.rollback() помечает все
загруженные ORM-объекты протухшими, и следующее обращение к любому их
полю тянет SELECT из синхронного кода — в асинхронном SQLAlchemy это
падает с MissingGreenlet. После редиректа запрос начинается с чистой
сессией, и протухших объектов просто нет.

Побочная польза: обновление страницы не отправляет форму повторно.
"""

from starlette.requests import Request

SESSION_KEY = "flashes"

KIND_ERROR = "error"
KIND_SUCCESS = "success"
KIND_WARN = "warn"


def flash(request: Request, text: str, kind: str = KIND_SUCCESS) -> None:
    messages = request.session.get(SESSION_KEY) or []
    messages.append({"kind": kind, "text": text})
    request.session[SESSION_KEY] = messages


def error(request: Request, text: str) -> None:
    flash(request, text, KIND_ERROR)


def success(request: Request, text: str) -> None:
    flash(request, text, KIND_SUCCESS)


def warn(request: Request, text: str) -> None:
    flash(request, text, KIND_WARN)


def pop_flashes(request: Request) -> list[dict]:
    """Забрать сообщения и очистить очередь — показываются они один раз."""
    messages = request.session.pop(SESSION_KEY, None) or []
    return messages
