# ===========================================================================
# app/api/dashboard.py
#
# Dashboard route — the home page of My Financial Life.
#
# GET /          — dashboard (default timescale MTD)
# GET /dashboard — same, explicit URL for nav link
# Both accept ?ts=<timescale> query parameter.
# ===========================================================================

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse

from app.core.templates import templates
from app.core.dashboard.dashboard import (
    get_dashboard_data,
    TIMESCALE_OPTIONS,
    DEFAULT_TIMESCALE,
)
from app.core.accounts.accounts import get_all_accounts

router = APIRouter()


def _dashboard_response(request: Request, ts: str):
    data           = get_dashboard_data(ts)
    account_groups = get_all_accounts()
    return templates.TemplateResponse(
        request,
        "dashboard/index.html",
        {
            "active":            "dashboard",
            "data":              data,
            "data_accounts":     account_groups,
            "timescale_options": TIMESCALE_OPTIONS,
            "chart_labels":  [c.label        for c in data.category_spending],
            "chart_amounts": [float(c.amount) for c in data.category_spending],
            "chart_colors":  [c.color         for c in data.category_spending],
        },
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard_root(
    request: Request,
    ts: str = Query(default=DEFAULT_TIMESCALE),
):
    return _dashboard_response(request, ts)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_explicit(
    request: Request,
    ts: str = Query(default=DEFAULT_TIMESCALE),
):
    return _dashboard_response(request, ts)
