# ===========================================================================
# app/api/settings.py
#
# FastAPI router for settings and profile routes.
#
# Routes:
#   GET  /settings/profile  — show the profile form
#   POST /settings/profile  — save profile and redirect to accounts
# ===========================================================================

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.templates import templates
from app.core.accounts.person import get_person, get_currencies, save_person

router = APIRouter()


@router.get("/settings/profile", response_class=HTMLResponse)
async def profile_get(request: Request):
    """Show the profile form, pre-filled if a profile already exists."""
    person     = get_person()
    currencies = get_currencies()

    return templates.TemplateResponse(
        request,
        "settings/profile.html",
        {
            "active":     "settings",
            "person":     person,
            "currencies": currencies,
            "saved":      request.query_params.get("saved") == "1",
        },
    )


@router.post("/settings/profile")
async def profile_post(
    request:            Request,
    first_name:         str = Form(...),
    last_name:          str = Form(...),
    base_currency_iri:  str = Form(...),
):
    """Save the profile and redirect back to the form with a success flag."""
    first_name  = first_name.strip()
    last_name   = last_name.strip()

    # Basic validation — both names required
    if not first_name or not last_name:
        person     = get_person()
        currencies = get_currencies()
        return templates.TemplateResponse(
            request,
            "settings/profile.html",
            {
                "active":     "settings",
                "person":     person,
                "currencies": currencies,
                "error":      "First name and last name are required.",
                "saved":      False,
            },
            status_code=422,
        )

    save_person(
        first_name=first_name,
        last_name=last_name,
        base_currency_iri=base_currency_iri,
    )

    return RedirectResponse(url="/settings/profile?saved=1", status_code=303)