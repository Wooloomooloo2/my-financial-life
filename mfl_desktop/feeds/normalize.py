"""Normalise provider transactions into the import pipeline's raw-txn dicts
(ADR-077, Arc H).

A raw-txn dict is what the OFX/CSV/QIF parsers emit and
``ImportService._classify_and_stage`` consumes: ``date`` (ISO), ``amount``
(positive magnitude), ``tx_type`` ('debit'/'credit'), ``payee_raw``, ``memo``,
and ``fitid`` (the stable provider id → dedup key). Mapping a feed onto this
shape is all it takes to reuse staging, FITID/hash dedup, the manual-match
heuristic, review, and commit.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation


def _first_line(text: str) -> str:
    return (text or "").splitlines()[0].strip() if text else ""


def _gc_row(t: dict) -> dict | None:
    """One GoCardless booked transaction → a raw-txn dict, or None if it lacks
    a usable amount/date."""
    amt = (t.get("transactionAmount") or {}).get("amount")
    try:
        amount = Decimal(str(amt))
    except (InvalidOperation, TypeError):
        return None
    date_iso = t.get("bookingDate") or t.get("valueDate")
    if not date_iso:
        return None
    # Payee: the counterparty name if present, else the first line of the
    # free-text remittance info (banks vary in which they populate).
    payee = (
        t.get("creditorName")
        or t.get("debtorName")
        or _first_line(t.get("remittanceInformationUnstructured", ""))
    )
    memo = t.get("remittanceInformationUnstructured", "") or ""
    # transactionId is the bank's stable id (best dedup key); fall back to the
    # provider-internal id. Empty → the import layer composite-hashes instead.
    fitid = t.get("transactionId") or t.get("internalTransactionId") or ""
    return {
        "date": str(date_iso),
        "amount": abs(amount),
        "tx_type": "debit" if amount < 0 else "credit",
        "payee_raw": payee or "",
        "memo": memo,
        "fitid": str(fitid),
    }


def _eb_remittance(t: dict) -> str:
    info = t.get("remittance_information")
    if isinstance(info, list):
        return " ".join(str(x) for x in info if x).strip()
    return str(info or "").strip()


def _eb_row(t: dict) -> dict | None:
    """One Enable Banking transaction → a raw-txn dict, or None if unusable."""
    amt = (t.get("transaction_amount") or {}).get("amount")
    try:
        amount = Decimal(str(amt))
    except (InvalidOperation, TypeError):
        return None
    date_iso = t.get("booking_date") or t.get("value_date")
    if not date_iso:
        return None
    # Enable Banking reports a magnitude + a separate CRDT/DBIT indicator.
    indicator = (t.get("credit_debit_indicator") or "").upper()
    tx_type = "credit" if indicator == "CRDT" else "debit"
    remittance = _eb_remittance(t)
    # The counterparty is the creditor on money-out, the debtor on money-in.
    counterparty = (
        (t.get("creditor") or {}).get("name")
        if tx_type == "debit"
        else (t.get("debtor") or {}).get("name")
    )
    payee = counterparty or _first_line(remittance)
    fitid = t.get("entry_reference") or t.get("transaction_id") or ""
    return {
        "date": str(date_iso)[:10],
        "amount": abs(amount),
        "tx_type": tx_type,
        "payee_raw": payee or "",
        "memo": remittance,
        "fitid": str(fitid),
    }


def normalize_enablebanking(transactions: list[dict]) -> list[dict]:
    """Enable Banking transaction rows → raw-txn dicts (booked only).

    Pending rows (``status`` other than ``BOOK``) are skipped — they mutate
    before posting, which would churn the FITID dedup. They land once booked.
    """
    rows: list[dict] = []
    for t in transactions or []:
        if (t.get("status") or "BOOK").upper() != "BOOK":
            continue
        row = _eb_row(t)
        if row is not None:
            rows.append(row)
    return rows


def _sf_row(t: dict) -> dict | None:
    """One SimpleFIN transaction → a raw-txn dict, or None if unusable."""
    try:
        amount = Decimal(str(t.get("amount")))
    except (InvalidOperation, TypeError):
        return None
    posted = t.get("posted")
    try:
        date_iso = datetime.datetime.fromtimestamp(
            int(posted), datetime.timezone.utc
        ).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    description = str(t.get("description", "") or "")
    payee = str(t.get("payee", "") or "") or description
    memo = str(t.get("memo", "") or "") or description
    return {
        "date": date_iso,
        "amount": abs(amount),
        # SimpleFIN amounts are signed: negative = money out.
        "tx_type": "debit" if amount < 0 else "credit",
        "payee_raw": payee,
        "memo": memo,
        "fitid": str(t.get("id") or ""),
    }


def normalize_simplefin(transactions: list[dict]) -> list[dict]:
    """SimpleFIN transaction rows (one account's ``transactions``) → raw-txn
    dicts. SimpleFIN only returns posted transactions, so there is no pending
    filter to apply."""
    rows: list[dict] = []
    for t in transactions or []:
        row = _sf_row(t)
        if row is not None:
            rows.append(row)
    return rows


def _plaid_row(t: dict) -> dict | None:
    """One Plaid transaction → a raw-txn dict, or None if unusable."""
    try:
        amount = Decimal(str(t.get("amount")))
    except (InvalidOperation, TypeError):
        return None
    date_iso = t.get("date") or t.get("authorized_date")
    if not date_iso:
        return None
    # Plaid's sign is inverted vs OFX: POSITIVE = money out (debit).
    tx_type = "debit" if amount > 0 else "credit"
    payee = str(t.get("merchant_name") or t.get("name") or "")
    memo = str(t.get("name") or "")
    return {
        "date": str(date_iso)[:10],
        "amount": abs(amount),
        "tx_type": tx_type,
        "payee_raw": payee,
        "memo": memo,
        "fitid": str(t.get("transaction_id") or ""),
    }


def normalize_plaid(transactions: list[dict], *, account_id: str | None = None) -> list[dict]:
    """Plaid transaction rows → raw-txn dicts (booked only).

    Pending rows (``pending`` true) are skipped — they re-post with a new
    ``transaction_id`` once settled, which the FITID dedup would otherwise
    double-count. Pass ``account_id`` to keep only one account's rows (a Plaid
    Item can span several accounts; each MFL feed links to one)."""
    rows: list[dict] = []
    for t in transactions or []:
        if t.get("pending"):
            continue
        if account_id is not None and t.get("account_id") != account_id:
            continue
        row = _plaid_row(t)
        if row is not None:
            rows.append(row)
    return rows


def normalize_gocardless(transactions: dict) -> list[dict]:
    """GoCardless ``{'booked': [...], 'pending': [...]}`` → raw-txn dicts.

    Booked only in v1: pending rows mutate (amount/id can change when they
    post), which would create churn/ghosts against the FITID dedup. They land
    naturally once booked.
    """
    rows: list[dict] = []
    for t in transactions.get("booked", []) or []:
        row = _gc_row(t)
        if row is not None:
            rows.append(row)
    return rows
