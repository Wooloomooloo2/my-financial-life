# ===========================================================================
# app/api/transactions.py
# ===========================================================================

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, Response

from app.core.transactions.transactions import (
    get_categories_for_select,
    update_transaction_field,
    bulk_update_transactions,
    delete_transaction,
    STATUS_OPTIONS,
    UNCAT_IRI,
    _load_category_labels,
    _load_category_families,
    STATUS_META,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# HTML fragment helpers
# ---------------------------------------------------------------------------

def _category_display_html(tx_key, cat_iri, cat_label, cat_color):
    return (
        f'<span class="{cat_color} cursor-pointer hover:underline"'
        f' hx-get="/transactions/{tx_key}/edit/category"'
        f' hx-target="#cat-{tx_key}" hx-swap="innerHTML" hx-trigger="click">'
        f'{cat_label}</span>'
    )


def _status_display_html(tx_key, status_label, status_badge):
    return (
        f'<span class="badge badge-sm {status_badge} cursor-pointer"'
        f' hx-get="/transactions/{tx_key}/edit/status"'
        f' hx-target="#status-{tx_key}" hx-swap="innerHTML" hx-trigger="click">'
        f'{status_label}</span>'
    )


def _text_display_html(tx_key, field, value, placeholder="—"):
    if value.strip():
        content = f'<span class="truncate block max-w-[180px]">{value}</span>'
    else:
        content = f'<span class="text-base-content/30">{placeholder}</span>'
    return (
        f'<span class="cursor-pointer hover:text-primary"'
        f' hx-get="/transactions/{tx_key}/edit/{field}"'
        f' hx-target="#{field}-{tx_key}" hx-swap="innerHTML" hx-trigger="click">'
        f'{content}</span>'
    )


def _category_options_html(categories, current_iri):
    uncat_sel = "selected" if not current_iri or current_iri == UNCAT_IRI else ""
    html = f'<option value="" {uncat_sel}>— Uncategorised —</option>'
    for group in categories:
        html += f'<optgroup label="{group.label}">'
        for item in group.items:
            sel = "selected" if item.iri == current_iri else ""
            html += f'<option value="{item.iri}" {sel}>{item.label}</option>'
        html += "</optgroup>"
    return html


# ---------------------------------------------------------------------------
# GET — edit widgets
# ---------------------------------------------------------------------------

@router.get("/transactions/{tx_key}/edit/category", response_class=HTMLResponse)
async def edit_category(tx_key: str, current: str = Query(default="")):
    categories = get_categories_for_select()
    options    = _category_options_html(categories, current)
    return HTMLResponse(
        f'<select name="value" class="select select-xs w-full min-w-[160px]"'
        f' hx-post="/transactions/{tx_key}/save/category"'
        f' hx-target="#cat-{tx_key}" hx-swap="innerHTML"'
        f' hx-trigger="change" hx-include="this" autofocus>'
        f'{options}</select>'
    )


@router.get("/transactions/{tx_key}/edit/status", response_class=HTMLResponse)
async def edit_status(tx_key: str, current: str = Query(default="")):
    options = "".join(
        f'<option value="{iri}" {"selected" if iri == current else ""}>{label}</option>'
        for iri, label in STATUS_OPTIONS
    )
    return HTMLResponse(
        f'<select name="value" class="select select-xs w-full"'
        f' hx-post="/transactions/{tx_key}/save/status"'
        f' hx-target="#status-{tx_key}" hx-swap="innerHTML"'
        f' hx-trigger="change" hx-include="this" autofocus>'
        f'{options}</select>'
    )


@router.get("/transactions/{tx_key}/edit/payee", response_class=HTMLResponse)
async def edit_payee(tx_key: str, current: str = Query(default="")):
    escaped = current.replace('"', "&quot;")
    return HTMLResponse(
        f'<input type="text" name="value" value="{escaped}"'
        f' class="input input-xs w-full min-w-[140px]"'
        f' hx-post="/transactions/{tx_key}/save/payee"'
        f' hx-target="#payee-{tx_key}" hx-swap="innerHTML"'
        f' hx-trigger="blur, keyup[key==\'Enter\']" hx-include="this" autofocus>'
    )


@router.get("/transactions/{tx_key}/edit/memo", response_class=HTMLResponse)
async def edit_memo(tx_key: str, current: str = Query(default="")):
    escaped = current.replace('"', "&quot;")
    return HTMLResponse(
        f'<input type="text" name="value" value="{escaped}"'
        f' class="input input-xs w-full min-w-[140px]"'
        f' hx-post="/transactions/{tx_key}/save/memo"'
        f' hx-target="#memo-{tx_key}" hx-swap="innerHTML"'
        f' hx-trigger="blur, keyup[key==\'Enter\']" hx-include="this" autofocus>'
    )


# ---------------------------------------------------------------------------
# POST — save and return display HTML
# ---------------------------------------------------------------------------

@router.post("/transactions/{tx_key}/save/category", response_class=HTMLResponse)
async def save_category(tx_key: str, value: str = Form(default="")):
    update_transaction_field(tx_key, "category", value)
    cat_labels   = _load_category_labels()
    cat_families = _load_category_families()
    cat_label    = cat_labels.get(value, "Uncategorised") if value else "Uncategorised"
    cat_fam      = cat_families.get(value, "uncat")        if value else "uncat"
    cat_color    = {
        "income":  "text-success text-xs",
        "expense": "text-base-content text-xs",
        "uncat":   "text-base-content/30 text-xs italic",
    }.get(cat_fam, "text-base-content/30 text-xs italic")
    return HTMLResponse(_category_display_html(tx_key, value, cat_label, cat_color))


@router.post("/transactions/{tx_key}/save/status", response_class=HTMLResponse)
async def save_status(tx_key: str, value: str = Form(default="")):
    update_transaction_field(tx_key, "status", value)
    s_label, s_badge = STATUS_META.get(value, ("Unknown", "badge-ghost"))
    return HTMLResponse(_status_display_html(tx_key, s_label, s_badge))


@router.post("/transactions/{tx_key}/save/payee", response_class=HTMLResponse)
async def save_payee(tx_key: str, value: str = Form(default="")):
    update_transaction_field(tx_key, "payee", value)
    return HTMLResponse(_text_display_html(tx_key, "payee", value))


@router.post("/transactions/{tx_key}/save/memo", response_class=HTMLResponse)
async def save_memo(tx_key: str, value: str = Form(default="")):
    update_transaction_field(tx_key, "memo", value)
    return HTMLResponse(_text_display_html(tx_key, "memo", value))


# ---------------------------------------------------------------------------
# DELETE — remove a single transaction
# ---------------------------------------------------------------------------

@router.delete("/transactions/{tx_key}")
async def delete_transaction_route(
    request:     Request,
    tx_key:      str,
    account_key: str = Query(default=""),
    return_url:  str = Query(default=""),
    page:        int = Query(default=1),
):
    """
    Delete a transaction and redirect the register.
    Prefers return_url (which carries full filter state) over the legacy
    account_key + page fallback.
    """
    delete_transaction(tx_key)
    r = Response(status_code=200)
    r.headers["HX-Redirect"] = return_url or f"/accounts/{account_key}?page={page}"
    return r


# ---------------------------------------------------------------------------
# POST — bulk update
# ---------------------------------------------------------------------------

@router.post("/transactions/bulk-update")
async def bulk_update(
    request:       Request,
    redirect_to:   str = Form(...),
    bulk_category: str = Form(default=""),
    bulk_status:   str = Form(default=""),
    bulk_payee:    str = Form(default=""),
    bulk_memo:     str = Form(default=""),
):
    form_data = await request.form()
    tx_keys   = form_data.getlist("tx_keys")

    if tx_keys:
        for field, value in [
            ("category", bulk_category),
            ("status",   bulk_status),
            ("payee",    bulk_payee),
            ("memo",     bulk_memo),
        ]:
            if value.strip():
                bulk_update_transactions(tx_keys, field, value)

    return Response(status_code=204, headers={"HX-Redirect": redirect_to})
