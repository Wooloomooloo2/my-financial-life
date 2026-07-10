# CSV transaction file parser with auto-format detection.
#
# Supported formats (auto-detected from file structure):
#   - Banktivity export  — account name on row 1, custom headers on row 2
#   - Credit card CSV    — ISO dates, debitCreditCode, merchant.name columns
#   - Generic bank CSV   — common column names mapped where possible
#
# Returns a list of normalised transaction dicts (same shape as ofx_parser.py),
# a has_status_override flag, and a format label.
#
# Lifted from app/core/import_engine/csv_parser.py with:
#   * Removed the Oxigraph/MFLX dependency — status_override is now a plain
#     enum string ("matched" / "reconciled" / "pending") instead of an IRI.
#   * Fixed a real syntax bug in parse_with_mapping (lines 120–125 of the v0.1
#     file were unindented and would never have parsed).
#   * Added category_raw field so the import service can parse hierarchical
#     category paths (Banktivity 'Parent:Child') rather than stuffing them in
#     the memo string.

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# Banktivity per-transaction status → the txn.status ladder (ADR-130). Their
# "cleared" is a bank-confirmed state, so it maps to our "matched".
_BANKTIVITY_STATUS_MAP = {
    "cleared":    "matched",
    "reconciled": "reconciled",
    "pending":    "pending",
}


# ── Column mapping for generic CSV ──────────────────────────────────────────


@dataclass
class CsvColumnMapping:
    date_col:        str
    date_format:     str  = "auto"
    amount_col:      str  = ""
    amount_inverted: bool = False
    debit_col:       str  = ""
    credit_col:      str  = ""
    payee_col:       str  = ""
    memo_col:        str  = ""
    category_col:    str  = ""


def header_signature(headers: list[str]) -> str:
    """A stable key for a CSV export's column layout (ADR-021 follow-up).

    Lower-cased, stripped, pipe-joined header row — same for every export of
    the same format, so a saved column mapping can be looked up and reused.
    Order is preserved (two banks with the same names in a different order are
    distinct layouts)."""
    return "|".join((h or "").strip().lower() for h in headers)


def header_signature_from_content(content: str) -> str:
    """Header signature read from the first row of decoded CSV content."""
    reader = csv.DictReader(io.StringIO(content))
    return header_signature(list(reader.fieldnames or []))


def parse_with_mapping(content: str, mapping: CsvColumnMapping) -> list[dict]:
    """Parse CSV content using an explicit column mapping."""
    rows = list(csv.DictReader(io.StringIO(content)))
    parse_date = make_generic_date_parser(
        (r.get(mapping.date_col, "") for r in rows),
    )
    transactions: list[dict] = []

    for row in rows:
        date_raw = row.get(mapping.date_col, "").strip()
        if mapping.date_format == "auto":
            date_iso = parse_date(date_raw)
        else:
            try:
                from datetime import datetime
                date_iso = datetime.strptime(date_raw, mapping.date_format).strftime("%Y-%m-%d")
            except ValueError:
                date_iso = parse_date(date_raw)
        if not date_iso:
            continue

        if mapping.amount_col:
            raw = (row.get(mapping.amount_col, "") or "").strip()
            value = _parse_amount_str(raw)
            if value is None:
                continue
            amount = abs(value)
            if mapping.amount_inverted:
                tx_type = "debit" if value > 0 else "credit"
            else:
                tx_type = "debit" if value < 0 else "credit"
        elif mapping.debit_col or mapping.credit_col:
            debit_raw  = (row.get(mapping.debit_col,  "") or "").strip()
            credit_raw = (row.get(mapping.credit_col, "") or "").strip()
            d_val = _parse_amount_str(debit_raw)
            c_val = _parse_amount_str(credit_raw)
            if d_val and abs(d_val) > 0:
                amount = abs(d_val); tx_type = "debit"
            elif c_val and abs(c_val) > 0:
                amount = abs(c_val); tx_type = "credit"
            else:
                continue
        else:
            continue

        payee_raw    = (row.get(mapping.payee_col,    "") or "").strip() if mapping.payee_col    else ""
        memo         = (row.get(mapping.memo_col,     "") or "").strip() if mapping.memo_col     else ""
        category_raw = (row.get(mapping.category_col, "") or "").strip() if mapping.category_col else ""

        transactions.append({
            "fitid":           "",
            "date":            date_iso,
            "amount":          amount,
            "tx_type":         tx_type,
            "payee_raw":       payee_raw,
            "memo":            memo,
            "status_override": "",
            "category_raw":    category_raw,
        })

    logger.info(f"Mapped CSV: parsed {len(transactions)} transactions")
    return transactions


def _parse_amount_str(s: str) -> Optional[Decimal]:
    clean = (
        s.strip().strip('"')
        .replace("£", "").replace("$", "").replace("€", "")
        .replace(",", "").strip()
    )
    if not clean:
        return None
    try:
        return Decimal(clean)
    except InvalidOperation:
        return None


# ── Public entry point ──────────────────────────────────────────────────────


def parse_csv(file_bytes: bytes, filename: str) -> tuple[list[dict], bool, str]:
    """Parse a CSV file into normalised transaction dicts.

    Returns (transactions, has_status_override, format_label).
    """
    content = _decode(file_bytes)
    lines = content.splitlines()
    if not lines:
        raise ValueError("File is empty.")

    fmt = _detect_format(lines)
    logger.info(f"Detected CSV format: {fmt} for {filename}")

    if fmt == "banktivity":
        txns, has_override = _parse_banktivity(lines)
        return txns, has_override, "Banktivity CSV"
    if fmt == "creditcard":
        txns = _parse_creditcard(content)
        return txns, False, "Credit Card CSV"
    txns = _parse_generic(content)
    return txns, False, "CSV"


# ── Format detection ────────────────────────────────────────────────────────


def _detect_format(lines: list[str]) -> str:
    if len(lines) < 2:
        return "generic"

    first = lines[0].strip()
    second = lines[1].strip().lower()
    headers = first.lower()

    if first.count(",") <= 2:
        if all(h in second for h in ("type", "status", "date", "payee")):
            return "banktivity"

    if "debitcreditcode" in headers or "merchant.name" in headers:
        return "creditcard"

    return "generic"


# ── Banktivity parser ───────────────────────────────────────────────────────


def _parse_banktivity(lines: list[str]) -> tuple[list[dict], bool]:
    if len(lines) < 3:
        raise ValueError("Banktivity file has too few rows.")
    header_line = lines[1]
    data_lines = lines[2:]
    reader = csv.DictReader(io.StringIO(header_line + "\n" + "\n".join(data_lines)))
    rows = _collapse_banktivity_splits(list(reader))

    transactions: list[dict] = []
    for row in rows:
        txn = _normalise_banktivity_row(row)
        if txn:
            transactions.append(txn)

    logger.info(f"Banktivity: parsed {len(transactions)} transactions")
    return transactions, True


def _collapse_banktivity_splits(rows: list[dict]) -> list[dict]:
    """Banktivity split transactions: a parent row has '(split)' in
    Category/Account, followed by sub-rows with empty Type/Status/Date/Payee,
    each carrying its own Category/Account, signed Amount, and (optionally) a
    Memo/Note.

    Collapse keeps the parent row and attaches the sub-rows as a structured
    ``_splits`` list (raw category / memo / signed-amount string). The
    normaliser turns that into real split lines (ADR-051) — they're no longer
    flattened into the memo. A '(split)' parent with no sub-rows comes out with
    an empty ``_splits`` and is treated as a plain Uncategorised transaction."""
    result: list[dict] = []
    pending: Optional[dict] = None
    split_lines: list[dict] = []

    def flush() -> None:
        nonlocal pending, split_lines
        if pending is not None:
            pending["_splits"] = split_lines
            result.append(pending)
        pending = None
        split_lines = []

    for row in rows:
        row_type = row.get("Type", "").strip()

        if not row_type:
            if pending is not None:
                cat = row.get("Category/Account", "").strip()
                amt = row.get("Amount", "").strip()
                memo = (
                    row.get("Memo", "").strip() or row.get("Note", "").strip()
                )
                if cat or amt:
                    split_lines.append({
                        "category_raw": cat,
                        "memo": memo,
                        "amount_raw": amt,
                    })
            continue

        flush()

        cat = row.get("Category/Account", "").strip()
        if cat == "(split)":
            pending = row
        else:
            result.append(row)

    flush()
    return result


def _normalise_banktivity_row(row: dict) -> Optional[dict]:
    row_type = row.get("Type", "").strip()
    if not row_type:
        return None

    date_str = row.get("Date", "").strip()
    try:
        date_iso = _parse_banktivity_date(date_str)
    except ValueError:
        logger.warning(f"Skipping row with unparseable date: {date_str!r}")
        return None

    amount_str = row.get("Amount", "").strip()
    try:
        amount, inferred_type = _parse_banktivity_amount(amount_str)
    except (InvalidOperation, ValueError):
        logger.warning(f"Skipping row with unparseable amount: {amount_str!r}")
        return None

    # Direction is taken from the amount sign — Banktivity exports
    # signed amounts across all three Types (Deposit, Withdrawal,
    # Transfer). Example: in bedford_house.csv, Withdrawals export as
    # ``-£X``; in ally_savings.csv, Transfers export as ``+$X`` for
    # inbound and ``-$X`` for outbound. The Type column itself is
    # therefore informational, not authoritative — see ADR-038. When
    # Type and sign disagree (a Withdrawal exported as ``+$X`` because
    # the user mis-tagged it in Banktivity), the sign is the user's
    # actual intent and gets the row's direction right. Type is still
    # surfaced into ``tx_type_label`` so future surfaces can warn on
    # mismatch; today it just logs.
    tx_type = inferred_type
    type_lower = row_type.lower()
    if (type_lower == "deposit" and tx_type == "debit") or (
        type_lower == "withdrawal" and tx_type == "credit"
    ):
        logger.info(
            f"Banktivity row Type={row_type!r} disagrees with amount "
            f"sign ({amount_str!r}); trusting sign per ADR-038."
        )

    payee_raw = row.get("Payee", "").strip()
    category = row.get("Category/Account", "").strip()
    note = row.get("Note", "").strip()
    memo_raw = row.get("Memo", "").strip()

    # Split transactions (ADR-051): build real split lines from the sub-rows
    # the collapser attached, instead of flattening them into the memo. Lines
    # carry signed amounts; if they don't sum to the parent total (Banktivity
    # quirk), append an Uncategorised remainder line rather than reject the row
    # — integer pence make the comparison exact, so no float tolerance.
    splits = _build_banktivity_splits(row.get("_splits"), amount, tx_type)

    # Memo: bank-supplied free text only. The category goes into category_raw
    # (parsed into the category hierarchy by the import service) — no longer
    # duplicated into the memo as the v0.1 service did.
    memo_parts: list[str] = []
    if note:
        memo_parts.append(note)
    if memo_raw:
        memo_parts.append(memo_raw)
    if row_type.lower() == "transfer":
        memo_parts.append(f"Transfer to {payee_raw}")
    memo = " | ".join(memo_parts)

    # Status override — Banktivity carries per-row Cleared/Reconciled status.
    status_raw = row.get("Status", "").strip().lower()
    status_override = _BANKTIVITY_STATUS_MAP.get(status_raw, "")

    # category_raw: pass the raw category string through. The import service
    # parses ':' separators into the hierarchical category tree. Drop the
    # '(split)' sentinel; for a split the parent's category isn't meaningful
    # (the lines carry the categories).
    category_raw = "" if (category in ("", "(split)") or splits) else category

    return {
        "fitid":           "",
        "date":            date_iso,
        "amount":          amount,
        "tx_type":         tx_type,
        "payee_raw":       payee_raw,
        "memo":            memo,
        "status_override": status_override,
        "category_raw":    category_raw,
        "splits":          splits,        # None unless this is a split (ADR-051)
    }


def _build_banktivity_splits(
    raw_splits: Optional[list[dict]], amount: Decimal, tx_type: str,
) -> Optional[list[dict]]:
    """Turn the collapser's raw sub-rows into signed split lines (ADR-051), or
    None when this isn't a split. ``amount``/``tx_type`` are the parent's
    magnitude + direction; the parent's signed total is reconstructed so a
    remainder line can balance the lines exactly."""
    if not raw_splits:
        return None
    parent_signed = amount if tx_type == "credit" else -amount
    lines: list[dict] = []
    line_sum = Decimal("0.00")
    for s in raw_splits:
        sval = _parse_amount_str(s.get("amount_raw", ""))
        if sval is None:
            logger.warning(
                "Skipping split line with unparseable amount: %r",
                s.get("amount_raw"),
            )
            continue
        sval = sval.quantize(Decimal("0.01"))
        cat = s.get("category_raw", "").strip()
        cat = "" if cat in ("", "(split)") else cat
        lines.append({
            "category_raw": cat,
            "memo": s.get("memo", "").strip(),
            "amount": sval,
        })
        line_sum += sval
    if not lines:
        return None
    remainder = (parent_signed - line_sum).quantize(Decimal("0.01"))
    if remainder != Decimal("0.00"):
        logger.warning(
            "Banktivity split lines sum to %s but the total is %s; adding an "
            "Uncategorised remainder line of %s.",
            line_sum, parent_signed, remainder,
        )
        lines.append({
            "category_raw": "",
            "memo": "Auto-balanced import remainder",
            "amount": remainder,
        })
    return lines


def _parse_banktivity_date(date_str: str) -> str:
    parts = date_str.strip().split("/")
    if len(parts) != 3:
        raise ValueError(f"Cannot parse date: {date_str!r}")
    month, day, year = parts
    year_int = int(year)
    if year_int < 100:
        year_int += 2000
    return f"{year_int:04d}-{int(month):02d}-{int(day):02d}"


def _parse_banktivity_amount(amount_str: str) -> tuple[Decimal, str]:
    # Delegate to the shared symbol-stripping helper so all three CSV
    # paths (generic mapping, Banktivity, generic-via-mapping at the
    # column-mapped fallback) accept the same symbol set: £, $, €. The
    # original Banktivity-specific helper only stripped £, which broke
    # USD Banktivity exports from US accounts (e.g. Chase via Banktivity)
    # — every row got skipped with "unparseable amount: '-$3.29'".
    value = _parse_amount_str(amount_str)
    if value is None:
        raise ValueError(f"Cannot parse amount: {amount_str!r}")
    return abs(value), ("debit" if value < 0 else "credit")


# ── Credit card parser ──────────────────────────────────────────────────────


def _parse_creditcard(content: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content))
    transactions: list[dict] = []

    for row in reader:
        date_raw = row.get("date", "").strip().strip('"')
        date_iso = date_raw[:10] if date_raw else ""
        if not date_iso or len(date_iso) != 10:
            continue

        amount_raw = row.get("amount", "").strip().strip('"')
        try:
            amount = Decimal(amount_raw)
        except InvalidOperation:
            logger.warning(f"Skipping row with unparseable amount: {amount_raw!r}")
            continue

        debit_credit = row.get("debitCreditCode", "").strip().lower()
        tx_type = "credit" if debit_credit == "credit" else "debit"

        payee_raw = (
            row.get("merchant.name", "").strip().strip('"')
            or row.get("description", "").strip().strip('"')
        )
        memo = row.get("description", "").strip().strip('"')

        transactions.append({
            "fitid":           "",
            "date":            date_iso,
            "amount":          amount,
            "tx_type":         tx_type,
            "payee_raw":       payee_raw,
            "memo":            memo,
            "status_override": "",
            "category_raw":    "",
        })

    logger.info(f"Credit card CSV: parsed {len(transactions)} transactions")
    return transactions


# ── Generic parser ──────────────────────────────────────────────────────────


_DATE_ALIASES   = ("date", "transaction date", "trans date", "value date")
_AMOUNT_ALIASES = ("amount", "transaction amount", "value")
_DEBIT_ALIASES  = ("debit", "debit amount", "withdrawals", "withdrawal")
_CREDIT_ALIASES = ("credit", "credit amount", "deposits", "deposit")
_PAYEE_ALIASES  = ("description", "payee", "reference", "details",
                   "transaction description", "narrative")
_MEMO_ALIASES   = ("memo", "note", "notes", "additional info")
# Used only by the mapping wizard's smart-default scan. The generic parser
# itself does not look for a category column — categories on the generic path
# come from the wizard's explicit mapping.
_CATEGORY_ALIASES = ("category", "categories", "tag", "tags")


def _parse_generic(content: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise ValueError("CSV file has no headers.")

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

    rows = list(reader)
    parse_date = make_generic_date_parser((r.get(date_col, "") for r in rows))

    transactions: list[dict] = []
    for row in rows:
        date_iso = parse_date(row.get(date_col, "").strip())
        if not date_iso:
            continue

        if amount_col:
            value = _parse_amount_str(row.get(amount_col, ""))
            if value is None:
                continue
            amount = abs(value)
            tx_type = "debit" if value < 0 else "credit"
        else:
            debit_value  = _parse_amount_str(row.get(debit_col,  ""))
            credit_value = _parse_amount_str(row.get(credit_col, ""))
            if debit_value is not None:
                amount = abs(debit_value)
                tx_type = "debit"
            elif credit_value is not None:
                amount = abs(credit_value)
                tx_type = "credit"
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
            "category_raw":    "",
        })

    logger.info(f"Generic CSV: parsed {len(transactions)} transactions")
    return transactions


def _find_col(headers: dict[str, str], aliases: tuple) -> Optional[str]:
    for alias in aliases:
        if alias in headers:
            return headers[alias]
    return None


def infer_day_first(samples: Iterable[str]) -> Optional[bool]:
    """Decide whether a whole date column is day-first or month-first.

    A single ``05/12/2021`` is undecidable, so a per-row format guess is a
    coin flip that silently corrupts half a US export (ADR-148). Read the
    column instead: a value whose *first* field exceeds 12 can only be
    day-first; one whose *second* field exceeds 12 can only be month-first.

    Returns True (day-first), False (month-first), or None when the column
    never disambiguates — every row could be read either way — or when it
    contradicts itself. The caller picks the fallback.
    """
    day_first_evidence = month_first_evidence = False
    for raw in samples:
        value = (raw or "").strip().strip('"')
        parts = re.split(r"[/-]", value)
        if len(parts) != 3:
            continue
        first, second, _ = parts
        if len(first) == 4:
            continue                      # ISO %Y-%m-%d — carries no vote
        if not (first.isdigit() and second.isdigit()):
            continue
        if int(first) > 12:
            day_first_evidence = True
        elif int(second) > 12:
            month_first_evidence = True
    if day_first_evidence == month_first_evidence:
        return None                       # no evidence, or self-contradictory
    return day_first_evidence


def _parse_generic_date(date_str: str, *, day_first: bool = True) -> str:
    """Parse one date cell. ``day_first`` orders the two ambiguous slash
    patterns; prefer ``make_generic_date_parser`` so the whole column decides
    it rather than each row racing to the first format that happens to fit."""
    from datetime import datetime
    date_str = date_str.strip().strip('"')
    ambiguous = (
        ["%d/%m/%Y", "%m/%d/%Y"] if day_first else ["%m/%d/%Y", "%d/%m/%Y"]
    )
    short = ["%d/%m/%y", "%m/%d/%y"] if day_first else ["%m/%d/%y", "%d/%m/%y"]
    formats = ["%Y-%m-%d", *ambiguous, "%d-%m-%Y", *short, "%Y%m%d"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def make_generic_date_parser(samples: Iterable[str]) -> Callable[[str], str]:
    """Build a date parser bound to one column's day/month order (ADR-148).

    Falls back to day-first when the column is undecidable, matching the
    historic behaviour, and says so in the log — an all-ambiguous column is
    the one case where the file genuinely cannot tell us.
    """
    day_first = infer_day_first(samples)
    if day_first is None:
        logger.warning(
            "CSV date column never disambiguates day from month "
            "(no value with a field > 12); assuming day-first. Pick an "
            "explicit date format in the mapping dialog if that is wrong."
        )
        day_first = True
    else:
        logger.info(
            f"CSV date column inferred as "
            f"{'day-first (DD/MM)' if day_first else 'month-first (MM/DD)'}"
        )
    return lambda s: _parse_generic_date(s, day_first=day_first)


# ── Encoding helper ─────────────────────────────────────────────────────────


def _decode(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "windows-1252", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(
        "Cannot decode file. Ensure it is a valid text (CSV) file."
    )
