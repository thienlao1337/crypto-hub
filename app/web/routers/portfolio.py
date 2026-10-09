"""Portfolio: summary, allocation, value history, trades."""

import json
import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.services import exchange_keys_service as keys_service
from app.services import portfolio_service, position_service
from app.web import auth
from app.web.templates_env import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("", response_class=HTMLResponse)
async def portfolio_page(
    request: Request,
    period: str = "7d",
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    if period not in portfolio_service.PERIODS:
        period = "7d"

    summary = await portfolio_service.build_summary(session, user)
    snapshots = await portfolio_service.history(session, user, period)
    positions = await position_service.list_positions(session, user)

    # The chart gets its data as a separate JSON block rather than via template
    # substitutions inside the script: that way values don't have to be escaped
    # by hand.
    chart_data = [
        {
            "time": snapshot.captured_at.isoformat(),
            "value": float(snapshot.total_usd),
        }
        for snapshot in snapshots
    ]

    return templates.TemplateResponse(
        request,
        "app/portfolio.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "summary": summary,
            "period": period,
            "periods": list(portfolio_service.PERIODS),
            "chart_json": json.dumps(chart_data),
            "donut": _donut_segments(summary),
            "positions": positions,
            "unrealized_total": position_service.total_unrealized(positions),
        },
    )


@router.get("/trades", response_class=HTMLResponse)
async def trades_page(
    request: Request,
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await portfolio_service.recent_trades(session, user, limit=200)

    return templates.TemplateResponse(
        request,
        "app/trades.html",
        {
            "current_user": user,
            "csrf_token": auth.issue_csrf_token(request),
            "rows": rows,
        },
    )


@router.post("/sync")
async def sync_now(
    request: Request,
    csrf_token: str = Form(""),
    user: User = Depends(auth.require_user),
    session: AsyncSession = Depends(get_session),
):
    """Sync balances on button press.

    Regular syncing is done by the background process; this is so you don't have to wait
    for the next cycle right after adding a key.
    """
    auth.verify_csrf(request, csrf_token)

    accounts = await keys_service.list_accounts(session, user)

    for account in accounts:
        adapter = await keys_service.build_adapter(session, account)
        try:
            await portfolio_service.sync_balances(session, account, adapter)
            await position_service.rebuild_positions(session, account)
            await keys_service.mark_synced(session, account)
        except Exception as exc:
            logger.warning("Sync of connection %s failed: %s", account.id, exc)
            await keys_service.mark_sync_error(session, account, str(exc))
        finally:
            await adapter.close()

    await position_service.mark_positions(session)
    await session.commit()
    return RedirectResponse("/portfolio", status_code=303)


def _donut_segments(summary: portfolio_service.PortfolioSummary) -> list[dict]:
    """Pie chart segments with cumulative boundaries.

    Boundaries are computed here, not in the template: conic-gradient needs cumulative
    percentages, and the arithmetic would read badly in Jinja. Small shares are merged
    into "other", otherwise the legend turns into a wall of text.
    """
    if summary.total_usd <= 0:
        return []

    threshold = Decimal("1.5")
    segments: list[dict] = []
    other = Decimal(0)

    for holding in summary.holdings:
        if holding.share_pct is None:
            continue
        if holding.share_pct < threshold:
            other += holding.share_pct
            continue
        segments.append({"label": holding.asset_symbol, "share": holding.share_pct})

    if other > 0:
        segments.append({"label": "прочее", "share": other})

    cursor = Decimal(0)
    for index, segment in enumerate(segments):
        segment["start"] = float(cursor)
        cursor += segment["share"]
        segment["end"] = float(cursor)
        segment["share"] = float(segment["share"])
        segment["index"] = index % 8

    return segments
