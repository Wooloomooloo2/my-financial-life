# ===========================================================================
# app/core/import_engine/import_service.py
#
# Import workflow: classify, stage in memory, commit to store.
#
# Workflow:
#   1. parse_and_stage()  — parse OFX, classify each transaction,
#                           store in _pending_imports under a token.
#   2. get_pending()      — retrieve staged import by token for preview.
#   3. commit_import()    — write to Oxigraph, clear from memory.
# ===========================================================================

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta, datetime
from decimal import Decimal
from typing import Optional

from pyoxigraph import NamedNode

from app.data.store import store
from app.core.ontology.namespaces import (
    DATA_GRAPH, MRL, MFLX, MFL,
    MFL_TRANSACTION, MFL_ON_ACCOUNT, MFL_AMOUNT,
    MFL_TRANSACTION_TYPE, MFL_TRANSACTION_STATUS,
    MFL_PAYEE_RAW, MFL_MEMO, MFL_IMPORT_HASH,
    MFL_IS_MANUAL_ENTRY,
    MFLX_TYPE_CREDIT, MFLX_TYPE_DEBIT,
    MFLX_STATUS_CLEARED, MFLX_STATUS_UNCLEARED,
)
from app.core.ontology.iri_factory import (
    iri_from_key, mfl_iri_from_key,
    new_transaction_iri, new_import_batch_iri,
)
from app.core.transactions.transactions import _fmt_date

logger = logging.getLogger(__name__)

XSD = "http://www.w3.org/2001/XMLSchema#"


# ---------------------------------------------------------------------------
# In-memory staging store  (single-user local app — no TTL needed)
# ---------------------------------------------------------------------------

_pending_imports: dict[str, "PendingImport"] = {}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ClassifiedTransaction:
    fitid:          str
    date_iso:       str
    amount:         Decimal
    tx_type:        str          # "debit" | "credit"
    payee_raw:      str
    memo:           str
    import_hash:    str          # FITID or computed hash — duplicate key
    status:         str          # "new" | "duplicate" | "potential_match"
    # Populated for potential_match only
    match_tx_key:   str  = ""
    match_tx_payee: str  = ""
    match_tx_date:  str  = ""
    # Status override from source file (e.g. Banktivity Cleared/Reconciled)
    # When set, takes priority over the user's global import status choice.
    status_override: str = ""
    # Pre-computed display strings
    date_display:   str  = ""
    amount_display: str  = ""
    amount_color:   str  = ""


@dataclass
class PendingImport:
    token:            str
    account_iri_key:  str
    account_name:     str
    filename:         str
    file_format:      str        # "OFX" | "QFX" | "Banktivity CSV" | "CSV" etc.
    transactions:     list[ClassifiedTransaction] = field(default_factory=list)
    new_count:        int = 0
    duplicate_count:  int = 0
    match_count:      int = 0
    is_first_import:  bool = True
    suggested_status: str = "cleared"
    currency_symbol:  str = "£"
    # True when the source file contains per-transaction status data.
    # When True, the preview hides the global status selector.
    has_status_override: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_hash(
    account_iri_key: str,
    date_iso:        str,
    amount:          Decimal,
    payee_raw:       str,
) -> str:
    """
    Compute a duplicate-detection hash for a CSV transaction.
    Used when no FITID is available (CSV imports).
    """
    raw = f"{account_iri_key}|{date_iso}|{amount}|{payee_raw}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')

def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _hash_exists(account_iri: NamedNode, import_hash: str) -> bool:
    """Return True if a transaction with this import hash exists on this account."""
    escaped = _esc(import_hash)
    sparql = f"""
        ASK {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?tx <{MFL_IMPORT_HASH.value}> "{escaped}"^^<{XSD}string> ;
                    <{MFL_ON_ACCOUNT.value}>  <{account_iri.value}> .
            }}
        }}
    """
    return bool(store.query(sparql))


def _find_manual_match(
    account_iri: NamedNode,
    date_iso:    str,
    amount:      Decimal,
    tx_type:     str,
) -> Optional[dict]:
    """
    Return a matching manually-entered transaction, or None.
    Matches on: same account + same amount + same type + date ±2 days.
    Only considers manual entries (not imported transactions).
    """
    try:
        d       = date.fromisoformat(date_iso)
        d_minus = (d - timedelta(days=2)).isoformat()
        d_plus  = (d + timedelta(days=2)).isoformat()
    except ValueError:
        return None

    type_iri = MFLX_TYPE_CREDIT.value if tx_type == "credit" else MFLX_TYPE_DEBIT.value

    sparql = f"""
        SELECT ?tx ?payeeRaw ?date
        WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?tx a <{MFL_TRANSACTION.value}> ;
                    <{MFL_ON_ACCOUNT.value}>         <{account_iri.value}> ;
                    <{MFL}transactionDate>            ?date ;
                    <{MFL_AMOUNT.value}>              "{amount}"^^<{XSD}decimal> ;
                    <{MFL_TRANSACTION_TYPE.value}>    <{type_iri}> ;
                    <{MFL_IS_MANUAL_ENTRY.value}>     "true"^^<{XSD}boolean> .
                OPTIONAL {{ ?tx <{MFL_PAYEE_RAW.value}> ?payeeRaw }}
                FILTER(
                    ?date >= "{d_minus}"^^<{XSD}date> &&
                    ?date <= "{d_plus}"^^<{XSD}date>
                )
                FILTER NOT EXISTS {{
                    ?tx <{MFL_IMPORT_HASH.value}> ?existingHash .
                }}
            }}
        }}
        LIMIT 1
    """
    for row in store.query(sparql):
        return {
            "tx_key":    row["tx"].value.split("#")[-1],
            "payee_raw": row["payeeRaw"].value if row["payeeRaw"] else "",
            "date_iso":  row["date"].value,
        }
    return None


def _is_first_import(account_iri: NamedNode) -> bool:
    """Return True if account has no non-opening-balance transactions."""
    sparql = f"""
        ASK {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?tx a <{MFL_TRANSACTION.value}> ;
                    <{MFL_ON_ACCOUNT.value}> <{account_iri.value}> .
                OPTIONAL {{ ?tx <{MFL_PAYEE_RAW.value}> ?payeeRaw }}
                FILTER(!BOUND(?payeeRaw) || STR(?payeeRaw) != "Opening Balance")
            }}
        }}
    """
    return not bool(store.query(sparql))


def _get_account_name(account_iri: NamedNode) -> str:
    from app.core.ontology.namespaces import MRL_ACCOUNT_NAME
    for quad in store.quads_for_pattern(account_iri, MRL_ACCOUNT_NAME, None, DATA_GRAPH):
        return quad.object.value
    return "Account"


def _get_currency_symbol(account_iri_key: str) -> str:
    from app.core.accounts.accounts import _get_currency_details
    from app.core.ontology.namespaces import MRL_ACCOUNT_CURRENCY
    account_iri = iri_from_key(account_iri_key)
    for quad in store.quads_for_pattern(account_iri, MRL_ACCOUNT_CURRENCY, None, DATA_GRAPH):
        _, symbol = _get_currency_details(quad.object)
        return symbol or "£"
    return "£"


# ---------------------------------------------------------------------------
# Stage (parse + classify)
# ---------------------------------------------------------------------------

def parse_and_stage(
    file_bytes:      bytes,
    filename:        str,
    account_iri_key: str,
) -> str:
    """
    Parse an OFX, QFX, or CSV file, classify each transaction, and stage
    in memory. Returns a token string for use in the preview/confirm steps.
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext in ("ofx", "qfx"):
        from app.core.import_engine.ofx_parser import parse_ofx
        raw_txns         = parse_ofx(file_bytes, filename)
        has_override     = False
        file_format      = "QFX" if ext == "qfx" else "OFX"
    elif ext == "csv":
        from app.core.import_engine.csv_parser import parse_csv
        raw_txns, has_override, file_format = parse_csv(file_bytes, filename)
    else:
        raise ValueError(
            f"Unsupported file format '.{ext}'. "
            "Please upload an OFX, QFX, or CSV file."
        )

    account_iri    = iri_from_key(account_iri_key)
    account_name   = _get_account_name(account_iri)
    currency_sym   = _get_currency_symbol(account_iri_key)
    first_import   = _is_first_import(account_iri)
    suggested      = "cleared" if first_import else "uncleared"

    classified: list[ClassifiedTransaction] = []
    new_count = dup_count = match_count = 0

    for raw in raw_txns:
        date_iso    = raw["date"]
        amount      = raw["amount"]
        tx_type     = raw["tx_type"]
        payee_raw   = raw.get("payee_raw", "")
        memo        = raw.get("memo", "")
        status_ov   = raw.get("status_override", "")

        # Duplicate key: FITID for OFX, computed hash for CSV
        fitid = raw.get("fitid", "")
        if fitid:
            import_hash = fitid
        else:
            import_hash = compute_hash(account_iri_key, date_iso,
                                       str(amount), payee_raw)
            fitid = import_hash

        # Display strings
        sym    = currency_sym
        is_deb = tx_type == "debit"
        amt_str   = f"−{sym}{amount:,.2f}" if is_deb else f"{sym}{amount:,.2f}"
        amt_color = "text-error" if is_deb else "text-base-content"

        # Classify
        if _hash_exists(account_iri, import_hash):
            status = "duplicate"
            dup_count += 1
            match_key = match_payee = match_date = ""
        else:
            manual = _find_manual_match(account_iri, date_iso, amount, tx_type)
            if manual:
                status      = "potential_match"
                match_key   = manual["tx_key"]
                match_payee = manual["payee_raw"]
                match_date  = _fmt_date(manual["date_iso"])
                match_count += 1
            else:
                status = "new"
                new_count += 1
                match_key = match_payee = match_date = ""

        classified.append(ClassifiedTransaction(
            fitid=fitid,
            date_iso=date_iso,
            amount=amount,
            tx_type=tx_type,
            payee_raw=payee_raw,
            memo=memo,
            import_hash=import_hash,
            status=status,
            match_tx_key=match_key,
            match_tx_payee=match_payee,
            match_tx_date=match_date,
            status_override=status_ov,
            date_display=_fmt_date(date_iso),
            amount_display=amt_str,
            amount_color=amt_color,
        ))

    token = uuid.uuid4().hex[:16]
    _pending_imports[token] = PendingImport(
        token=token,
        account_iri_key=account_iri_key,
        account_name=account_name,
        filename=filename,
        file_format=file_format,
        transactions=classified,
        new_count=new_count,
        duplicate_count=dup_count,
        match_count=match_count,
        is_first_import=first_import,
        suggested_status=suggested,
        currency_symbol=currency_sym,
        has_status_override=has_override,
    )
    return token


def get_pending(token: str) -> Optional[PendingImport]:
    return _pending_imports.get(token)


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def commit_import(
    token:                str,
    import_status:        str,        # "cleared" or "uncleared"
    accepted_match_fitids: set[str],  # fitids the user chose to merge
) -> tuple[NamedNode, int, int, int]:
    """
    Write the staged import to the Oxigraph store.
    Returns (batch_iri, imported_count, skipped_count, matched_count).
    """
    pending = _pending_imports.get(token)
    if not pending:
        raise ValueError("Import session not found or expired.")

    account_iri = iri_from_key(pending.account_iri_key)
    batch_iri   = new_import_batch_iri()
    status_iri  = (MFLX_STATUS_CLEARED if import_status == "cleared"
                   else MFLX_STATUS_UNCLEARED)

    imported = skipped = matched = 0

    for tx in pending.transactions:
        if tx.status == "duplicate":
            skipped += 1
            continue

        if tx.status == "potential_match":
            if tx.fitid in accepted_match_fitids:
                _merge_manual_transaction(tx.match_tx_key, tx)
                matched += 1
                continue
            # User rejected merge — import as new transaction

        # Use per-transaction status override if available
        if tx.status_override:
            effective_status = NamedNode(tx.status_override)
        else:
            effective_status = status_iri

        _write_transaction(account_iri, tx, batch_iri, effective_status)
        imported += 1

    _write_import_batch(
        batch_iri, account_iri, pending.filename, pending.file_format,
        len(pending.transactions), imported, skipped,
    )

    del _pending_imports[token]
    logger.info(
        f"Import committed: {imported} imported, {skipped} skipped, "
        f"{matched} matched — batch {batch_iri.value}"
    )
    return batch_iri, imported, skipped, matched


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _write_transaction(
    account_iri: NamedNode,
    tx:          ClassifiedTransaction,
    batch_iri:   NamedNode,
    status_iri:  NamedNode,
) -> None:
    tx_iri   = new_transaction_iri()
    type_iri = MFLX_TYPE_CREDIT if tx.tx_type == "credit" else MFLX_TYPE_DEBIT

    memo_triple = (
        f'<{tx_iri.value}> <{MFL_MEMO.value}> "{_esc(tx.memo)}"^^<{XSD}string> .'
        if tx.memo else ""
    )

    store.update(f"""
        INSERT DATA {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{tx_iri.value}> a <{MFL_TRANSACTION.value}> ;
                    <{MFL_ON_ACCOUNT.value}>        <{account_iri.value}> ;
                    <{MFL}transactionDate>           "{tx.date_iso}"^^<{XSD}date> ;
                    <{MFL_AMOUNT.value}>             "{tx.amount}"^^<{XSD}decimal> ;
                    <{MFL_TRANSACTION_TYPE.value}>   <{type_iri.value}> ;
                    <{MFL_TRANSACTION_STATUS.value}> <{status_iri.value}> ;
                    <{MFL_PAYEE_RAW.value}>          "{_esc(tx.payee_raw)}"^^<{XSD}string> ;
                    <{MFL_IMPORT_HASH.value}>        "{_esc(tx.import_hash)}"^^<{XSD}string> ;
                    <{MFL}importBatch>               <{batch_iri.value}> ;
                    <{MFL_IS_MANUAL_ENTRY.value}>    "false"^^<{XSD}boolean> .
                {memo_triple}
            }}
        }}
    """)


def _merge_manual_transaction(
    tx_key: str,
    ofx_tx: ClassifiedTransaction,
) -> None:
    """
    Update an existing manual transaction with OFX import data.
    Adds the import hash (prevents future duplicate imports) and memo.
    Does not change amount, date, category, or status set by user.
    """
    tx_iri = mfl_iri_from_key(tx_key)

    # Add import hash
    store.update(f"""
        INSERT DATA {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{tx_iri.value}> <{MFL_IMPORT_HASH.value}>
                    "{_esc(ofx_tx.import_hash)}"^^<{XSD}string> .
            }}
        }}
    """)

    # Add OFX memo only if no memo exists yet
    if ofx_tx.memo:
        existing_memo = any(
            True for _ in
            store.quads_for_pattern(tx_iri, MFL_MEMO, None, DATA_GRAPH)
        )
        if not existing_memo:
            store.update(f"""
                INSERT DATA {{
                    GRAPH <{DATA_GRAPH.value}> {{
                        <{tx_iri.value}> <{MFL_MEMO.value}>
                            "{_esc(ofx_tx.memo)}"^^<{XSD}string> .
                    }}
                }}
            """)


def _write_import_batch(
    batch_iri:   NamedNode,
    account_iri: NamedNode,
    filename:    str,
    file_format: str,
    total:       int,
    new_count:   int,
    dup_count:   int,
) -> None:
    now        = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    fmt_iri    = (MFLX + "ImportFormat_QFX" if file_format == "QFX"
                  else MFLX + "ImportFormat_OFX")

    store.update(f"""
        INSERT DATA {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{batch_iri.value}> a <{MFL}ImportBatch> ;
                    <{MFL}importDate>            "{now}"^^<{XSD}dateTime> ;
                    <{MFL}importFormat>          <{fmt_iri}> ;
                    <{MFL}importFileName>        "{_esc(filename)}"^^<{XSD}string> ;
                    <{MFL}importTargetAccount>   <{account_iri.value}> ;
                    <{MFL}importTransactionCount> "{total}"^^<{XSD}integer> ;
                    <{MFL}importNewCount>         "{new_count}"^^<{XSD}integer> ;
                    <{MFL}importDuplicateCount>   "{dup_count}"^^<{XSD}integer> .
            }}
        }}
    """)
