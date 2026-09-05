"""Главный экран. Пока заглушка — виджеты появятся с готовыми разделами."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.models import User
from app.web import auth
from app.web.templates_env import templates

router = APIRouter(tags=["dashboard"])


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(auth.require_user)):
    return templates.TemplateResponse(
        request,
        "app/dashboard.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
        },
    )
