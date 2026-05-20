# ===========================================================================
# app/core/import_engine/csv_parser.py
#
# CSV transaction file parser with auto-format detection.
#
# Supported formats (auto-detected from file structure):
#   - Banktivity export  — account name on row 1, custom headers on row 2
#   - Credit card CSV    — ISO dates, debitCreditCode, merchant.name columns
#   - Generic bank CSV   — common column names mapped where possible
#
# Returns a list of normalised transaction dicts (same structure as
# ofx_parser.py) plus a has_status_override flag and format label.
#
# Status override:
#   Banktivity exports include per-transaction status (Cleared/Reconciled).
#   When has_status_override=True, each dict may contain a "status_override"
#   key mapped to an mflx: status IRI. The import service uses this instead
#   of the user's global import status choice.
#
# Short-term transfer handling:
#   Transfer rows are imported as Debit transactions. The transfer destination
#   is stored in the memo field. Full double-entry transfer posting is a
#   post-MVP feature (see backlog).
# ===========================================================================

from __future__ import annotations

import csv
import io
import hashlib
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.core.ontology.namespaces import MFLX

logger = logging.getLogger(__name__)

# Status IRI mapping for Banktivity statuses
_BANKTIVITY_STATUS_MAP = {
    "cleared":    MFLX + "TransactionStatus_Cleared",
    "reconciled": MFLX + "TransactionStatus_Reconciled",
    "pending":    MFLX + "TransactionStatus_Pending",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_csv(
    file_bytes: bytes,
    filename:   str,
) -> tuple[list[dict], bool, str]:
    """
    Parse a CSV file into normalised transaction dicts.

    Returns:
        (transactions, has_status_override, format_label)

        transactions      — list of dicts, same keys as ofx_parser output plus
                            optional "status_override" (mflx IRI string).
        has_status_override — True if per-transaction status data is available.
        format_label      — human-readable format name for the preview.
    """
    content = _decode(file_bytes)
    lines   = content.splitlines()

    if not lines:
        raise ValueError("File is empty.")

    fmt = _detect_format(lines)
    logger.info(f"Detected CSV format: {fmt} for {filename}")

    if fmt == "banktivity":
        txns, has_override = _parse_banktivity(lines)
        return txns, has_override, "Banktivity CSV"
    elif fmt == "creditcard":
        txns = _parse_creditcard(content)
        return txns, False, "Credit Card CSV"
    else:
        txns = _parse_generic(content)
        return txns, False, "CSV"


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def _detect_format(lines: list[str]) -> str:
    if len(lines) < 2:
        return "generic"

    first   = lines[0].strip()
    second  = lines[1].strip().lower()
    headers = first.lower()

    # Banktivity: row 1 is an account name (few or no commas),
    # row 2 has Type, Status, Date, Payee headers.
    first_comma_count = first.count(",")
    if first_comma_count <= 2:
        if all(h in second for h in ("type", "status", "date", "payee")):
            return "banktivity"

    # Credit card: headers contain debitcreditcode or merchant.name
    if "debitcreditcode" in headers or "merchant.name" in headers:
        return "creditcard"

    return "generic"


# ---------------------------------------------------------------------------
# Banktivity parser
# ---------------------------------------------------------------------------

def _parse_banktivity(lines: list[str]) -> tuple[list[dict], bool]:
    """
    Parse a Banktivity export CSV.
    Returns (transactions, has_status_override=True).
    """
    # Row 0 = account name, row 1 = headers, rows 2+ = data
    if len(lines) < 3:
        raise ValueError("Banktivity file has too few rows.")

    header_line = lines[1]
    data_lines  = lines[2:]

    reader  = csv.DictReader(io.StringIO(header_line + "\n" + "\n".join(data_lines)))
    rows    = list(reader)

    # Collapse split transactions
    rows = _collapse_banktivity_splits(rows)

    transactions = []
    for row in rows:
        txn = _normalise_banktivity_row(row)
        if txn:
            transactions.append(txn)

    logger.info(f"Banktivity: parsed {len(transactions)} transactions")
    return transactions, True


def _collapse_banktivity_splits(rows: list[dict]) -> list[dict]:
    """
    Banktivity split transactions: a parent row has '(split)' in
    Category/Account, followed by sub-rows with empty Type/Status/Date/Payee.
    Collapse: keep parent total, append sub-row categories to memo.
    """
    result:       list[dict] = []
    pending:      Optional[dict] = None
    split_parts:  list[str] = []

    for row in rows:
        row_type = row.get("Type", "").strip()

        if not row_type:
            # Sub-row of a split — collect category info
            if pending is not None:
                cat = row.get("Category/Account", "").strip()
                amt = row.get("Amount", "").strip()
                if cat:
                    split_parts.append(f"{cat} ({amt})")
            continue

        # Emit any pending split before processing next row
        if pending is not None:
            if split_parts:
                pending["_split_memo"] = " | ".join(split_parts)
            result.append(pending)
            pending = None
            split_parts = []

        cat = row.get("Category/Account", "").strip()
        if cat == "(split)":
            pending = row
        else:
            result.append(row)

    # Flush last pending split
    if pending is not None:
        if split_parts:
            pending["_split_memo"] = " | ".join(split_parts)
        result.append(pending)

    return result


def _normalise_banktivity_row(row: dict) -> Optional[dict]:
    """Normalise a single Banktivity row to a standard transaction dict."""
    row_type = row.get("Type", "").strip()
    if not row_type:
        return None  # skip orphaned sub-rows

    # Date: M/D/YY format
    date_str = row.get("Date", "").strip()
    try:
        date_iso = _parse_banktivity_date(date_str)
    except ValueError:
        logger.warning(f"Skipping row with unparseable date: {date_str!r}")
        return None

    # Amount and direction
    amount_str = row.get("Amount", "").strip()
    try:
        amount, inferred_type = _parse_banktivity_amount(amount_str)
    except (InvalidOperation, ValueError):
        logger.warning(f"Skipping row with unparseable amount: {amount_str!r}")
        return None

    # Direction from Type column takes priority over amount sign
    if row_type.lower() == "deposit":
        tx_type = "credit"
    elif row_type.lower() in ("withdrawal", "transfer"):
        tx_type = "debit"
    else:
        tx_type = inferred_type

    payee_raw = row.get("Payee", "").strip()
    category  = row.get("Category/Account", "").strip()
    note      = row.get("Note", "").strip()
    memo_raw  = row.get("Memo", "").strip()
    split_memo = row.get("_split_memo", "").strip()

    # Build memo: combine category, note, bank memo, split detail
    memo_parts = []
    if category and category not in ("", "(split)"):
        memo_parts.append(category)
    if note:
        memo_parts.append(note)
    if memo_raw:
        memo_parts.append(memo_raw)
    if split_memo:
        memo_parts.append(f"Split: {split_memo}")
    if row_type.lower() == "transfer":
        memo_parts.append(f"Transfer to {payee_raw}")
    memo = " | ".join(memo_parts)

    # Status: honour Banktivity status
    status_raw = row.get("Status", "").strip().lower()
    status_override = _BANKTIVITY_STATUS_MAP.get(status_raw, "")

    return {
        "fitid":           "",        # no FITID for CSV
        "date":            date_iso,
        "amount":          amount,
        "tx_type":         tx_type,
        "payee_raw":       payee_raw,
        "memo":            memo,
        "status_override": status_override,
    }


def _parse_banktivity_date(date_str: str) -> str:
    """Parse M/D/YY → YYYY-MM-DD. Handles M/D/YY and M/D/YYYY."""
    parts = date_str.strip().split("/")
    if len(parts) != 3:
        raise ValueError(f"Cannot parse date: {date_str!r}")
    month, day, year = parts
    year_int = int(year)
    if year_int < 100:
        year_int += 2000
    return f"{year_int:04d}-{int(month):02d}-{int(day):02d}"


def _parse_banktivity_amount(amount_str: str) -> tuple[Decimal, str]:
    """
    Parse Banktivity amount string. Returns (abs_amount, tx_type).
    Handles: -£27.00, £5,810.72, "-£1,089.98", etc.
    """
    clean = (
        amount_str.strip()
        .strip('"')
        .replace("£", "")
        .replace(",", "")
        .strip()
    )
    value = Decimal(clean)
    return abs(value), ("debit" if value < 0 else "credit")


# ---------------------------------------------------------------------------
# Credit card parser
# ---------------------------------------------------------------------------

def _parse_creditcard(content: str) -> list[dict]:
    """
    Parse the credit card CSV format with merchant.name and debitCreditCode.
    """
    reader = csv.DictReader(io.StringIO(content))
    transactions = []

    for row in reader:
        # Date: ISO 8601 with time  e.g. "2026-05-11T00:00:00Z"
        date_raw = row.get("date", "").strip().strip('"')
        date_iso = date_raw[:10] if date_raw else ""
        if not date_iso or len(date_iso) != 10:
            continue

        # Amount: already positive decimal
        amount_raw = row.get("amount", "").strip().strip('"')
        try:
            amount = Decimal(amount_raw)
        except InvalidOperation:
            logger.warning(f"Skipping row with unparseable amount: {amount_raw!r}")
            continue

        # Direction
        debit_credit = row.get("debitCreditCode", "").strip().lower()
        tx_type = "credit" if debit_credit == "credit" else "debit"

        # Payee: merchant.name is cleaner than description
        payee_raw = (
            row.get("merchant.name", "").strip().strip('"')
            or row.get("description", "").strip().strip('"')
        )

        # Memo: full raw description
        memo = row.get("description", "").strip().strip('"')

        transactions.append({
            "fitid":           "",
            "date":            date_iso,
            "amount":          amount,
            "tx_type":         tx_type,
            "payee_raw":       payee_raw,
            "memo":            memo,
            "status_override": "",
        })

    logger.info(f"Credit card CSV: parsed {len(transactions)} transactions")
    return transactions


# ---------------------------------------------------------------------------
# Generic parser
# ---------------------------------------------------------------------------

# Common column name aliases for generic bank CSVs
_DATE_ALIASES    = ("date", "transaction date", "trans date", "value date")
_AMOUNT_ALIASES  = ("amount", "transaction amount", "value")
_DEBIT_ALIASES   = ("debit", "debit amount", "withdrawals", "withdrawal")
_CREDIT_ALIASES  = ("credit", "credit amount", "deposits", "deposit")
_PAYEE_ALIASES   = ("description", "payee", "reference", "details",
                    "transaction description", "narrative")
_MEMO_ALIASES    = ("memo", "note", "notes", "additional info")


def _parse_generic(content: str) -> list[dict]:
    """
    Parse a generic bank CSV by matching common column name patterns.
    Works for many UK bank exports (Nationwide, Lloyds, Barclays simplified).
    """
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise ValueError("CSV file has no headers.")

    # Normalise header names for matching
    headers = {h.strip().lower(): h for h in reader.fieldnames if h}

    date_col   = _find_col(headers, _DATE_ALIASES)
    amount_col = _find_col(headers, _AMOUNT_ALIASES)
    debit_col  = _find_col(headers, _DEBIT_ALIASES)
    credit_col = _find_col(headers, _CREDIT_ALIASES)
    payee_col  = _find_col(headers, _PAYEE_ALIASES)
    memo_col   = _find_col(headers, _MEMO_ALIASES)

    if not date_col:
        raise ValueError(
            "Could not find a date column. "
            f"Available columns: {', '.join(reader.fieldnames)}"
        )
    if not amount_col and not (debit_col and credit_col):
        raise ValueError(
            "Could not find an amount column. "
            f"Available columns: {', '.join(reader.fieldnames)}"
        )

    transactions = []
    for row in reader:
        date_iso = _parse_generic_date(row.get(date_col, "").strip())
        if not date_iso:
            continue

        # Amount — handle single column or separate debit/credit columns
        if amount_col:
            amount_str = row.get(amount_col, "").strip()
            try:
                value  = Decimal(amount_str.replace(",", "").replace("£", "")
                                 .replace("$", "").strip())
                amount = abs(value)
                tx_type = "debit" if value < 0 else "credit"
            except InvalidOperation:
                continue
        else:
            debit_str  = row.get(debit_col,  "").strip().replace(",", "")
            credit_str = row.get(credit_col, "").strip().replace(",", "")
            if debit_str:
                try:
                    amount  = abs(Decimal(debit_str.replace("£", "").replace("$", "")))
                    tx_type = "debit"
                except InvalidOperation:
                    continue
            elif credit_str:
                try:
                    amount  = abs(Decimal(credit_str.replace("£", "").replace("$", "")))
                    tx_type = "credit"
                except InvalidOperation:
                    continue
            else:
                continue

        payee_raw = row.get(payee_col, "").strip() if payee_col else ""
        memo      = row.get(memo_col,  "").strip() if memo_col  else ""

        transactions.append({
            "fitid":           "",
            "date":            date_iso,
            "amount":          amount,
            "tx_type":         tx_type,
            "payee_raw":       payee_raw,
            "memo":            memo,
            "status_override": "",
        })

    logger.info(f"Generic CSV: parsed {len(transactions)} transactions")
    return transactions


def _find_col(headers: dict[str, str], aliases: tuple) -> Optional[str]:
    """Return the actual column name that matches any alias, or None."""
    for alias in aliases:
        if alias in headers:
            return headers[alias]
    return None


def _parse_generic_date(date_str: str) -> str:
    """
    Try common date formats: YYYY-MM-DD, DD/MM/YYYY, MM/DD/YYYY, DD-MM-YYYY.
    Returns ISO date string or empty string if unrecognised.
    """
    from datetime import datetime
    date_str = date_str.strip().strip('"')

    formats = [
        "%Y-%m-%d",   # ISO
        "%d/%m/%Y",   # UK
        "%m/%d/%Y",   # US full year
        "%d-%m-%Y",
        "%d/%m/%y",   # UK short year
        "%m/%d/%y",   # US short year
        "%Y%m%d",     # compact
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------------------
# Encoding helper
# ---------------------------------------------------------------------------

def _decode(file_bytes: bytes) -> str:
    """Decode file bytes, trying common encodings."""
    for encoding in ("utf-8-sig", "utf-8", "windows-1252", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(
        "Cannot decode file. Ensure it is a valid text (CSV) file."
    )
