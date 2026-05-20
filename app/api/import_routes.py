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
    """Show the file upload form."""
    grouped = get_all_accounts()
    # Flatten to a single list for the account dropdown
    all_accounts = [
        acc
        for accounts in grouped.values()
        for acc in accounts
    ]
    return templates.TemplateResponse(
        request,
        "import/upload.html",
        {
            "active":      "import",
            "accounts":    all_accounts,
            "preselect":   account,
            "error":       None,
        },
    )


# ---------------------------------------------------------------------------
# Parse — upload file and classify transactions
# ---------------------------------------------------------------------------

@router.post("/import/parse")
async def import_parse(
    request:     Request,
    account_key: str        = Form(...),
    file:        UploadFile = File(...),
):
    """Parse uploaded OFX/QFX file and redirect to preview."""
    filename  = file.filename or "import.ofx"
    file_bytes = await file.read()

    if not file_bytes:
        grouped     = get_all_accounts()
        all_accounts = [a for accs in grouped.values() for a in accs]
        return templates.TemplateResponse(
            request,
            "import/upload.html",
            {
                "active":    "import",
                "accounts":  all_accounts,
                "preselect": account_key,
                "error":     "The uploaded file is empty.",
            },
            status_code=422,
        )

    try:
        token = parse_and_stage(file_bytes, filename, account_key)
    except ValueError as e:
        grouped      = get_all_accounts()
        all_accounts = [a for accs in grouped.values() for a in accs]
        return templates.TemplateResponse(
            request,
            "import/upload.html",
            {
                "active":    "import",
                "accounts":  all_accounts,
                "preselect": account_key,
                "error":     str(e),
            },
            status_code=422,
        )

    return RedirectResponse(
        url=f"/import/preview/{token}", status_code=303
    )


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

@router.get("/import/preview/{token}", response_class=HTMLResponse)
async def import_preview(request: Request, token: str):
    """Show import preview — new transactions, matches, and duplicates."""
    pending = get_pending(token)
    if not pending:
        return RedirectResponse(url="/import?error=expired", status_code=303)

    new_txns     = [t for t in pending.transactions if t.status == "new"]
    matches      = [t for t in pending.transactions if t.status == "potential_match"]
    duplicate_ct = pending.duplicate_count

    return templates.TemplateResponse(
        request,
        "import/preview.html",
        {
            "active":       "import",
            "pending":      pending,
            "new_txns":     new_txns,
            "matches":      matches,
            "duplicate_ct": duplicate_ct,
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
    import_status: str = Form(...),   # "cleared" or "uncleared"
):
    """Commit the import and redirect to the result page."""
    pending = get_pending(token)
    if not pending:
        return RedirectResponse(url="/import?error=expired", status_code=303)

    form_data     = await request.form()
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
    account_key = pending.account_iri_key

    return RedirectResponse(
        url=(
            f"/import/result"
            f"?account={account_key}"
            f"&imported={imported}"
            f"&skipped={skipped}"
            f"&matched={matched}"
            f"&batch={batch_key}"
            f"&filename={pending.filename}"
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
    """Post-import summary page."""
    return templates.TemplateResponse(
        request,
        "import/result.html",
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
