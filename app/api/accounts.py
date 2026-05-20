# ===========================================================================
# app/api/accounts.py
#
# FastAPI router for account management.
#
# Routes:
#   GET  /accounts              — accounts list grouped by family
#   GET  /accounts/new          — add account form
#   POST /accounts/new          — create account and redirect to list
#   GET  /accounts/type-fields  — HTMX partial: type-specific form fields
# ===========================================================================

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.templates import templates
from app.core.accounts.accounts import (
    get_all_accounts,
    create_account,
    ACCOUNT_TYPE_OPTIONS,
    ACCOUNT_FAMILIES,
    get_type_option,
)
from app.core.accounts.person import get_currencies
from app.core.transactions.transactions import (
    get_account_detail,
    get_transactions_for_account,
    get_categories_for_select,
    STATUS_OPTIONS,
)

router = APIRouter()


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request):
    """Show all accounts grouped by family."""
    grouped   = get_all_accounts()
    families  = ACCOUNT_FAMILIES

    return templates.TemplateResponse(
        request,
        "accounts/list.html",
        {
            "active":   "accounts",
            "grouped":  grouped,
            "families": families,
        },
    )


@router.get("/accounts/new", response_class=HTMLResponse)
async def account_new_get(
    request: Request,
    family:  str = Query(default=""),
):
    """Show the add account form. Optionally pre-selects a family."""
    currencies   = get_currencies()
    today        = date.today().isoformat()

    return templates.TemplateResponse(
        request,
        "accounts/new.html",
        {
            "active":       "accounts",
            "type_options": ACCOUNT_TYPE_OPTIONS,
            "currencies":   currencies,
            "today":        today,
            "preselect_family": family,
            "error":        None,
        },
    )


@router.post("/accounts/new")
async def account_new_post(
    request:           Request,
    account_type_key:  str = Form(...),
    account_name:      str = Form(...),
    currency_iri:      str = Form(...),
    opening_balance:   str = Form(default="0.00"),
    opening_date:      str = Form(...),
    notes:             str = Form(default=""),
    # Cash fields
    interest_rate:     str = Form(default=""),
    # Credit fields
    credit_limit:      str = Form(default=""),
    statement_day:     str = Form(default=""),
    # Investment fields
    growth_rate:       str = Form(default="0"),
    dividend_rate:     str = Form(default="0"),
    reinvest_dividends: str = Form(default=""),
    # Property fields
    property_address:  str = Form(default=""),
    purchase_price:    str = Form(default=""),
    purchase_date:     str = Form(default=""),
    is_mortgaged:      str = Form(default=""),
):
    """Create a new account and redirect to the accounts list."""

    # Validate
    errors = []
    if not account_name.strip():
        errors.append("Account name is required.")
    if not account_type_key:
        errors.append("Account type is required.")
    if not opening_date:
        errors.append("Opening balance date is required.")

    # Parse opening balance
    try:
        balance = Decimal(opening_balance.strip() or "0")
    except InvalidOperation:
        errors.append("Opening balance must be a number.")
        balance = Decimal("0")

    if errors:
        currencies = get_currencies()
        today      = date.today().isoformat()
        return templates.TemplateResponse(
            request,
            "accounts/new.html",
            {
                "active":           "accounts",
                "type_options":     ACCOUNT_TYPE_OPTIONS,
                "currencies":       currencies,
                "today":            today,
                "preselect_family": "",
                "error":            " ".join(errors),
            },
            status_code=422,
        )

    create_account(
        account_type_key   = account_type_key,
        account_name       = account_name.strip(),
        currency_iri       = currency_iri,
        opening_balance    = balance,
        opening_date       = opening_date,
        notes              = notes,
        interest_rate      = interest_rate or None,
        credit_limit       = credit_limit or None,
        statement_day      = statement_day or None,
        growth_rate        = growth_rate or "0",
        dividend_rate      = dividend_rate or "0",
        reinvest_dividends = bool(reinvest_dividends),
        property_address   = property_address,
        purchase_price     = purchase_price or None,
        purchase_date      = purchase_date or None,
        is_mortgaged       = bool(is_mortgaged),
    )

    return RedirectResponse(url="/accounts?added=1", status_code=303)


@router.get("/accounts/type-fields", response_class=HTMLResponse)
async def account_type_fields(
    request:          Request,
    account_type_key: str = Query(default=""),
):
    option = get_type_option(account_type_key)
    today  = date.today().isoformat()
    return templates.TemplateResponse(
        request,
        "accounts/_type_fields.html",
        {"option": option, "today": today},
    )


@router.get("/accounts/{iri_key}", response_class=HTMLResponse)
async def account_register(request: Request, iri_key: str):
    """Transaction register for a single account."""
    account = get_account_detail(iri_key)
    if not account:
        return templates.TemplateResponse(
            request, "accounts/list.html",
            {"active": "accounts", "grouped": {}, "families": ACCOUNT_FAMILIES},
            status_code=404,
        )
    transactions = get_transactions_for_account(account)
    categories   = get_categories_for_select()
    return templates.TemplateResponse(
        request,
        "transactions/register.html",
        {
            "active":         "accounts",
            "account":        account,
            "transactions":   transactions,
            "categories":     categories,
            "status_options": STATUS_OPTIONS,
            "today":          date.today().isoformat(),
        },
    )
# ===========================================================================
# ADDITION — append this route to the bottom of:
# app/api/accounts.py
# ===========================================================================

@router.post("/accounts/{iri_key}/add-transaction")
async def add_transaction(
    request:   Request,
    iri_key:   str,
    date:      str     = Form(...),
    payee_raw: str     = Form(...),
    amount:    str     = Form(...),
    tx_type:   str     = Form(...),
):
    """Add a single manual transaction to an account."""
    from decimal import Decimal, InvalidOperation
    from app.core.ontology.iri_factory import iri_from_key
    from app.core.transactions.transactions import create_manual_transaction

    try:
        amt = Decimal(amount.strip())
    except InvalidOperation:
        amt = Decimal("0")

    account_iri = iri_from_key(iri_key)
    create_manual_transaction(
        account_iri = account_iri,
        date_str    = date,
        payee_raw   = payee_raw.strip(),
        amount      = amt,
        tx_type     = tx_type,
    )
    return RedirectResponse(
        url=f"/accounts/{iri_key}", status_code=303
    )