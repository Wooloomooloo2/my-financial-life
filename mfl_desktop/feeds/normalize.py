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
