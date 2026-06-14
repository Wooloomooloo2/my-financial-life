"""Pure compute for the Payee report (ADR-066 / Arc E, E2).

No Qt, no SQL. The Repository hands in per-canonical-payee spending totals
already FX-converted to one display currency (see
:meth:`Repository.payee_spending_aggregates`); this module:

- turns the raw aggregate rows into sorted :class:`PayeeSpendRow` records
  (pence → major units, descending by amount, with each payee's share of
  the grand total);
- keeps the top ``top_n`` payees (the long tail is simply not shown — no
  "Other" bucket; the summary still reports how many payees were hidden);
- derives the headline summary (grand total, distinct payee count, top
  payee).

Spending is a **positive magnitude** in major units (pounds of the display
currency). Mirrors the shape of :mod:`mfl_desktop.reports.income_expense` —
pure, fully verifiable offscreen.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

# Shown for the no-payee group — the Repository returns ``payee_id = None``
# for transactions whose ``payee_id`` is NULL.
NO_PAYEE_LABEL = "(No payee)"

_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class PayeeSpendRow:
    """One row in the ranked chart / table.

    ``payee_id`` is the canonical payee id, or ``None`` for the no-payee
    group. ``amount`` is a non-negative Decimal in the display currency's
    major units; ``pct`` is the row's share of the grand total (across all
    payees, shown or not) in ``[0, 1]`` (0 when the total is 0).
    """
    payee_id: Optional[int]
    name: str
    amount: Decimal
    txn_count: int
    pct: float


@dataclass(frozen=True)
class PayeeReportSummary:
    """Headline figures for the right-hand summary panel."""
    total: Decimal                 # grand total spend across ALL payees
    payee_count: int               # distinct payees contributing
    shown_count: int               # rows displayed (after the top-N cap)
    hidden_count: int              # payees beyond the cap, not shown
    top_name: Optional[str]        # biggest payee by spend, None if no data
    top_amount: Decimal


@dataclass(frozen=True)
class PayeeReportResult:
    """Everything the window needs to render: the display rows + summary."""
    rows: list[PayeeSpendRow]
    summary: PayeeReportSummary


def _pct(amount: Decimal, total: Decimal) -> float:
    return float(amount / total) if total > 0 else 0.0


def build_report(raw_payees: list[dict], top_n: int) -> PayeeReportResult:
    """Rank the raw aggregate rows by spend and keep the top ``top_n``.

    ``raw_payees`` is ``Repository.payee_spending_aggregates()['payees']`` —
    a list of ``{payee_id, name, spending_pence, txn_count}`` dicts, one per
    canonical payee, in arbitrary order. ``top_n`` caps how many payees are
    shown; the remainder is simply omitted (no "Other" bucket) and counted
    in ``summary.hidden_count``. ``top_n <= 0`` shows every payee.

    Percentages are each row's share of the **grand total across all
    payees** (shown or hidden), so a row's pct is its true share of spend
    and the shown rows can sum to less than 100% when the tail is hidden.
    The summary's ``total`` is likewise the all-payees grand total.
    """
    # Pence → major units, drop zero/negative defensively, sort by spend desc
    # then name for a stable tie-break.
    rows_all: list[tuple[Optional[int], str, Decimal, int]] = []
    for r in raw_payees:
        pence = int(r.get("spending_pence", 0))
        if pence <= 0:
            continue
        name = r.get("name") or NO_PAYEE_LABEL
        rows_all.append(
            (r.get("payee_id"), name, Decimal(pence) / _HUNDRED,
             int(r.get("txn_count", 0))),
        )
    rows_all.sort(key=lambda t: (-t[2], t[1].lower()))

    total = sum((t[2] for t in rows_all), Decimal(0))
    payee_count = len(rows_all)

    head = rows_all[:top_n] if (top_n and top_n > 0) else rows_all
    rows: list[PayeeSpendRow] = [
        PayeeSpendRow(
            payee_id=pid, name=name, amount=amount, txn_count=count,
            pct=_pct(amount, total),
        )
        for (pid, name, amount, count) in head
    ]

    if rows_all:
        top_name: Optional[str] = rows_all[0][1]
        top_amount = rows_all[0][2]
    else:
        top_name, top_amount = None, Decimal(0)

    summary = PayeeReportSummary(
        total=total,
        payee_count=payee_count,
        shown_count=len(rows),
        hidden_count=payee_count - len(rows),
        top_name=top_name,
        top_amount=top_amount,
    )
    return PayeeReportResult(rows=rows, summary=summary)
