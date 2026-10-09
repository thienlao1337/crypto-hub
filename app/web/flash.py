"""Messages between requests.

Needed for the "submit - redirect - show" pattern: after an error the form handler
doesn't render the page itself, it puts the message here and sends the browser to a GET.

This solves a specific problem: session.rollback() marks all loaded ORM objects as
expired, and the next access to any of their fields triggers a SELECT from synchronous
code - in async SQLAlchemy that fails with MissingGreenlet. After a redirect the request
starts with a clean session, and there are simply no expired objects.

A side benefit: refreshing the page doesn't resubmit the form.
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
    """Take the messages and clear the queue - they're shown once."""
    messages = request.session.pop(SESSION_KEY, None) or []
    return messages
