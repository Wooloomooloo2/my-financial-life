# ===========================================================================
# app/api/import_routes.py
#
# Routes for the OFX/QFX import workflow.
#
# GET  /import                   — upload form
# POST /import/parse             — parse file, redirect to preview
# GET  /import/preview/{token}   — preview before confirming
# POST /import/confirm/{token}   — commit import, redirect to result
# GET  /import/result            — post-import summary
# ===========================================================================

from fastapi import APIRouter, Request, Form, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.templates import templates
from app.core.accounts.accounts import get_all_accounts
from app.core.import_engine.import_service import (
    parse_and_stage,
    get_pending,
    get_pending_map,
    apply_mapping_and_stage,
    commit_import,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Upload form
# ---------------------------------------------------------------------------

@router.get("/import", response_class=HTMLResponse)
async def import_upload_get(
    request: Request,
    account: str = Query(default=""),
):
    grouped      = get_all_accounts()
    all_accounts = [a for accs in grouped.values() for a in accs]
    return templates.TemplateResponse(
        request, "import/upload.html",
        {"active": "import", "accounts": all_accounts,
         "preselect": account, "error": None},
    )


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

@router.post("/import/parse")
async def import_parse(
    request:     Request,
    account_key: str        = Form(...),
    file:        UploadFile = File(...),
):
    filename   = file.filename or "import.ofx"
    file_bytes = await file.read()

    if not file_bytes:
        grouped      = get_all_accounts()
        all_accounts = [a for accs in grouped.values() for a in accs]
        return templates.TemplateResponse(
            request, "import/upload.html",
            {"active": "import", "accounts": all_accounts,
             "preselect": account_key, "error": "The uploaded file is empty."},
            status_code=422,
        )

    try:
        token, next_step = parse_and_stage(file_bytes, filename, account_key)
    except ValueError as e:
        grouped      = get_all_accounts()
        all_accounts = [a for accs in grouped.values() for a in accs]
        return templates.TemplateResponse(
            request, "import/upload.html",
            {"active": "import", "accounts": all_accounts,
             "preselect": account_key, "error": str(e)},
            status_code=422,
        )

    if next_step == "map":
        return RedirectResponse(url=f"/import/map/{token}", status_code=303)
    return RedirectResponse(url=f"/import/preview/{token}", status_code=303)


# ---------------------------------------------------------------------------
# Column mapping (generic CSV)
# ---------------------------------------------------------------------------

@router.get("/import/map/{token}", response_class=HTMLResponse)
async def import_map_get(request: Request, token: str):
    """Show column mapping form for unrecognised CSV files."""
    pending_map = get_pending_map(token)
    if not pending_map:
        return RedirectResponse(url="/import?error=expired", status_code=303)

    # Build column options with sample values for each header
    col_options = []
    for i, h in enumerate(pending_map.headers):
        sample = pending_map.preview_rows[0][i] if pending_map.preview_rows else ""
        col_options.append({"name": h, "sample": sample})

    return templates.TemplateResponse(
        request, "import/map.html",
        {
            "active":      "import",
            "token":       token,
            "pending_map": pending_map,
            "col_options": col_options,
        },
    )


@router.post("/import/map/{token}")
async def import_map_post(
    request:         Request,
    token:           str,
    date_col:        str  = Form(...),
    date_format:     str  = Form(default="auto"),
    amount_mode:     str  = Form(default="single"),
    amount_col:      str  = Form(default=""),
    amount_inverted: str  = Form(default=""),
    debit_col:       str  = Form(default=""),
    credit_col:      str  = Form(default=""),
    payee_col:       str  = Form(default=""),
    memo_col:        str  = Form(default=""),
    category_col:    str  = Form(default=""),
):
    """Apply column mapping and redirect to preview."""
    from app.core.import_engine.csv_parser import CsvColumnMapping

    mapping = CsvColumnMapping(
        date_col        = date_col,
        date_format     = date_format,
        amount_col      = amount_col      if amount_mode == "single" else "",
        amount_inverted = bool(amount_inverted),
        debit_col       = debit_col       if amount_mode == "split"  else "",
        credit_col      = credit_col      if amount_mode == "split"  else "",
        payee_col       = payee_col,
        memo_col        = memo_col,
        category_col    = category_col,
    )

    try:
        import_token = apply_mapping_and_stage(token, mapping)
    except ValueError as e:
        return RedirectResponse(
            url=f"/import/map/{token}?error={e}", status_code=303
        )

    return RedirectResponse(url=f"/import/preview/{import_token}", status_code=303)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

@router.get("/import/preview/{token}", response_class=HTMLResponse)
async def import_preview(request: Request, token: str):
    pending = get_pending(token)
    if not pending:
        return RedirectResponse(url="/import?error=expired", status_code=303)

    return templates.TemplateResponse(
        request, "import/preview.html",
        {
            "active":       "import",
            "pending":      pending,
            "new_txns":     [t for t in pending.transactions if t.status == "new"],
            "matches":      [t for t in pending.transactions if t.status == "potential_match"],
            "duplicate_ct": pending.duplicate_count,
            "token":        token,
        },
    )


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------

@router.post("/import/confirm/{token}")
async def import_confirm(
    request:       Request,
    token:         str,
    import_status: str = Form(...),
):
    pending = get_pending(token)
    if not pending:
        return RedirectResponse(url="/import?error=expired", status_code=303)

    form_data       = await request.form()
    accepted_fitids = set(form_data.getlist("accept_match"))

    try:
        batch_iri, imported, skipped, matched = commit_import(
            token, import_status, accepted_fitids
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/import/preview/{token}?error={e}", status_code=303
        )

    batch_key = batch_iri.value.split("#")[-1]

    return RedirectResponse(
        url=(
            f"/import/result"
            f"?account={pending.account_iri_key}"
            f"&imported={imported}&skipped={skipped}&matched={matched}"
            f"&batch={batch_key}&filename={pending.filename}"
        ),
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@router.get("/import/result", response_class=HTMLResponse)
async def import_result(
    request:  Request,
    account:  str = Query(default=""),
    imported: int = Query(default=0),
    skipped:  int = Query(default=0),
    matched:  int = Query(default=0),
    batch:    str = Query(default=""),
    filename: str = Query(default=""),
):
    return templates.TemplateResponse(
        request, "import/result.html",
        {
            "active":      "import",
            "account_key": account,
            "imported":    imported,
            "skipped":     skipped,
            "matched":     matched,
            "batch_key":   batch,
            "filename":    filename,
        },
    )
