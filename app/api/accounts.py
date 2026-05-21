# ===========================================================================
# app/api/accounts.py
# ===========================================================================

from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, quote

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.templates import templates
from app.core.accounts.accounts import (
    get_all_accounts,
    create_account,
    delete_account,
    ACCOUNT_TYPE_OPTIONS,
    ACCOUNT_FAMILIES,
    get_type_option,
)
from app.core.accounts.person import get_currencies
from app.core.transactions.transactions import (
    get_account_detail,
    get_transactions_for_account,
    get_transaction_count,
    get_categories_for_select,
    STATUS_OPTIONS,
    FilterParams,
    DATE_PRESETS,
)

router = APIRouter()


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(request: Request):
    grouped  = get_all_accounts()
    families = ACCOUNT_FAMILIES
    return templates.TemplateResponse(
        request, "accounts/list.html",
        {"active": "accounts", "grouped": grouped, "families": families},
    )


@router.get("/accounts/new", response_class=HTMLResponse)
async def account_new_get(request: Request, family: str = Query(default="")):
    currencies = get_currencies()
    today      = date.today().isoformat()
    return templates.TemplateResponse(
        request, "accounts/new.html",
        {
            "active":           "accounts",
            "type_options":     ACCOUNT_TYPE_OPTIONS,
            "currencies":       currencies,
            "today":            today,
            "preselect_family": family,
            "error":            None,
        },
    )


@router.post("/accounts/new")
async def account_new_post(
    request:            Request,
    account_type_key:   str = Form(...),
    account_name:       str = Form(...),
    currency_iri:       str = Form(...),
    opening_balance:    str = Form(default="0.00"),
    opening_date:       str = Form(...),
    notes:              str = Form(default=""),
    interest_rate:      str = Form(default=""),
    credit_limit:       str = Form(default=""),
    statement_day:      str = Form(default=""),
    growth_rate:        str = Form(default="0"),
    dividend_rate:      str = Form(default="0"),
    reinvest_dividends: str = Form(default=""),
    property_address:   str = Form(default=""),
    purchase_price:     str = Form(default=""),
    purchase_date:      str = Form(default=""),
    is_mortgaged:       str = Form(default=""),
):
    errors = []
    if not account_name.strip():
        errors.append("Account name is required.")
    if not account_type_key:
        errors.append("Account type is required.")
    if not opening_date:
        errors.append("Opening balance date is required.")

    try:
        balance = Decimal(opening_balance.strip() or "0")
    except InvalidOperation:
        errors.append("Opening balance must be a number.")
        balance = Decimal("0")

    if errors:
        currencies = get_currencies()
        today      = date.today().isoformat()
        return templates.TemplateResponse(
            request, "accounts/new.html",
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
        account_type_key    = account_type_key,
        account_name        = account_name.strip(),
        currency_iri        = currency_iri,
        opening_balance     = balance,
        opening_date        = opening_date,
        notes               = notes,
        interest_rate       = interest_rate or None,
        credit_limit        = credit_limit or None,
        statement_day       = statement_day or None,
        growth_rate         = growth_rate or "0",
        dividend_rate       = dividend_rate or "0",
        reinvest_dividends  = bool(reinvest_dividends),
        property_address    = property_address,
        purchase_price      = purchase_price or None,
        purchase_date       = purchase_date or None,
        is_mortgaged        = bool(is_mortgaged),
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
        request, "accounts/_type_fields.html",
        {"option": option, "today": today},
    )


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

def _make_url(
    base:     str,
    filters:  FilterParams,
    page:     int,
    per_page: int,
    sort_col: str | None = None,
    sort_dir: str | None = None,
) -> str:
    """
    Build a register URL that preserves all active filter state.
    sort/dir are omitted when they equal the defaults ('date'/'desc') to keep
    the URL clean. page and per_page are always included.
    """
    sc = sort_col if sort_col is not None else filters.sort_col
    sd = sort_dir if sort_dir is not None else filters.sort_dir

    params: dict[str, str] = {}
    if filters.search:      params["search"]   = filters.search
    if filters.date_preset: params["date"]      = filters.date_preset
    if filters.status:      params["status"]    = filters.status
    if filters.category:    params["category"]  = filters.category
    if sc != "date":        params["sort"]      = sc
    if sd != "desc":        params["dir"]       = sd
    params["page"]     = str(page)
    params["per_page"] = str(per_page)

    return base + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

@router.get("/accounts/{iri_key}", response_class=HTMLResponse)
async def account_register(
    request:     Request,
    iri_key:     str,
    page:        int = Query(default=1),
    per_page:    int = Query(default=50),
    search:      str = Query(default=""),
    date_preset: str = Query(default="", alias="date"),
    status:      str = Query(default=""),
    category:    str = Query(default=""),
    sort:        str = Query(default="date"),
    dir:         str = Query(default="desc"),
):
    account = get_account_detail(iri_key)
    if not account:
        return templates.TemplateResponse(
            request, "accounts/list.html",
            {"active": "accounts", "grouped": {}, "families": ACCOUNT_FAMILIES},
            status_code=404,
        )

    _valid_presets = {p for p, _ in DATE_PRESETS}

    filters = FilterParams(
        search      = search.strip(),
        date_preset = date_preset if date_preset in _valid_presets else "",
        status      = status,
        category    = category,
        sort_col    = sort if sort in {"date", "payee", "amount", "category"} else "date",
        sort_dir    = dir  if dir  in {"asc", "desc"}                          else "desc",
    )

    rows, total = get_transactions_for_account(
        account, page=page, per_page=per_page, filters=filters
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    page        = max(1, min(page, total_pages))
    page_start  = (page - 1) * per_page + 1 if total else 0
    page_end    = min(page * per_page, total)
    categories  = get_categories_for_select()
    today       = date.today().isoformat()

    base = f"/accounts/{iri_key}"

    # Sort URL for each sortable column: toggles direction if already active,
    # otherwise uses the natural default for that column.
    def _sort_url(col: str) -> str:
        if filters.sort_col == col:
            new_dir = "asc" if filters.sort_dir == "desc" else "desc"
        else:
            new_dir = "desc" if col in ("date", "amount") else "asc"
        return _make_url(base, filters, 1, per_page, sort_col=col, sort_dir=new_dir)

    # Per-page selector base URL (JS appends 'page=1&per_page=N')
    pp_params: dict[str, str] = {}
    if filters.search:             pp_params["search"]   = filters.search
    if filters.date_preset:        pp_params["date"]      = filters.date_preset
    if filters.status:             pp_params["status"]    = filters.status
    if filters.category:           pp_params["category"]  = filters.category
    if filters.sort_col != "date": pp_params["sort"]      = filters.sort_col
    if filters.sort_dir != "desc": pp_params["dir"]       = filters.sort_dir
    pp_qs            = urlencode(pp_params)
    perpage_base_url = f"{base}?{pp_qs}&" if pp_qs else f"{base}?"

    # URL for clearing all filters while preserving current sort
    _clear_f          = FilterParams(sort_col=filters.sort_col, sort_dir=filters.sort_dir)
    clear_filters_url = _make_url(base, _clear_f, 1, per_page)

    # Full URL for the current page (used in bulk redirect and delete return)
    current_page_url         = _make_url(base, filters, page, per_page)
    current_page_url_encoded = quote(current_page_url, safe="")

    return templates.TemplateResponse(
        request, "transactions/register.html",
        {
            "active":                    "accounts",
            "account":                   account,
            "transactions":              rows,
            "total":                     total,
            "page":                      page,
            "per_page":                  per_page,
            "total_pages":               total_pages,
            "page_start":                page_start,
            "page_end":                  page_end,
            "categories":                categories,
            "status_options":            STATUS_OPTIONS,
            "today":                     today,
            # filter/sort state
            "filters":                   filters,
            "date_presets":              DATE_PRESETS,
            # precomputed URLs
            "sort_urls":                 {c: _sort_url(c) for c in ("date", "payee", "amount", "category")},
            "prev_page_url":             _make_url(base, filters, page - 1, per_page) if page > 1 else None,
            "next_page_url":             _make_url(base, filters, page + 1, per_page) if page < total_pages else None,
            "perpage_base_url":          perpage_base_url,
            "clear_filters_url":         clear_filters_url,
            "current_page_url":          current_page_url,
            "current_page_url_encoded":  current_page_url_encoded,
        },
    )


@router.post("/accounts/{iri_key}/add-transaction")
async def add_transaction(
    request:   Request,
    iri_key:   str,
    date_str:  str = Form(..., alias="date"),
    payee_raw: str = Form(...),
    amount:    str = Form(...),
    tx_type:   str = Form(...),
):
    from decimal import Decimal, InvalidOperation
    from app.core.ontology.iri_factory import iri_from_key
    from app.core.transactions.transactions import create_manual_transaction

    account_iri = iri_from_key(iri_key)
    try:
        amt = Decimal(amount.strip())
    except InvalidOperation:
        amt = Decimal("0")

    create_manual_transaction(
        account_iri = account_iri,
        date_str    = date_str,
        payee_raw   = payee_raw.strip(),
        amount      = amt,
        tx_type     = tx_type,
    )
    return RedirectResponse(url=f"/accounts/{iri_key}", status_code=303)


@router.post("/accounts/{iri_key}/delete")
async def delete_account_route(request: Request, iri_key: str):
    """Delete an account and all its transactions. Redirects to accounts list."""
    delete_account(iri_key)
    return RedirectResponse(url="/accounts", status_code=303)
