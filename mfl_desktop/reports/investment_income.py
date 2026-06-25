"""Investment Income — pure aggregation for the per-security yield table and the
trailing-period income bar chart (ADR-108).

No Qt, no SQL. Walks an account's transaction rows and books *income* — cash
dividends / interest / cap-gain distributions (``is_income``) plus, optionally,
reinvested distributions (``is_reinvest``, valued like the holdings engine:
``abs(amount)`` when a cash figure is present, else
``quantity × price × multiplier``). Units are the holdings engine's
major-currency floats (``holdings._to_money`` does not rescale), so a security's
income here lines up with ``compute_returns``' ``dividends_window`` when
reinvests are included.

Currency-agnostic, exactly like ``compute_returns``: the caller runs one pass
per currency group and converts the results into the display currency. The
window (``InvestmentIncomeWindow``) does that conversion.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mfl_desktop.import_engine.qif_actions import is_income, is_reinvest


@dataclass(frozen=True)
class IncomeFilters:
    """The Investment Income view's filter state.

    Not persisted — ADR-108 ships no saved-report type (hence no migration);
    the view is a live analysis window, so these live only for the session.
    ``period_key`` defaults to ``"1y"`` = trailing twelve months (TTM).
    ``account_ids`` empty == the whole portfolio.
    """
    period_key: str = "1y"
    custom_start: Optional[str] = None
    custom_end: Optional[str] = None
    account_ids: tuple[int, ...] = ()
    include_reinvested: bool = True

    @classmethod
    def default(cls) -> "IncomeFilters":
        return cls()


def income_for_txn(t, multipliers: dict[int, float], include_reinvested: bool) -> float:
    """Native-currency income booked by one transaction, or ``0.0`` when it is
    not an income event. Mirrors the holdings engine (ADR-046 / 089 / 093):

    - cash income (``is_income``) = the positive cash ``amount``;
    - a reinvested distribution (``is_reinvest``) = ``abs(amount)`` when a cash
      figure is present, else ``quantity × price × multiplier`` (the lot cost
      the engine books for a no-cash reinvest), counted only when
      ``include_reinvested``.
    """
    action = t.action
    if is_income(action):
        return float(t.amount or 0.0)
    if include_reinvested and is_reinvest(action):
        if t.amount is not None and float(t.amount) != 0.0:
            return abs(float(t.amount))
        if t.quantity is not None and t.price is not None:
            mult = (
                float(multipliers.get(t.security_id, 1.0))
                if t.security_id is not None else 1.0
            )
            return float(t.quantity) * float(t.price) * mult
    return 0.0


def _in_window(posted_date: Optional[str], start_iso: str, end_iso: str) -> bool:
    return bool(posted_date) and start_iso <= posted_date <= end_iso


def income_by_security(
    txns, start_iso: str, end_iso: str,
    multipliers: dict[int, float], include_reinvested: bool,
) -> dict[int, float]:
    """``security_id → total income`` over ``[start_iso, end_iso]`` (native ccy)."""
    out: dict[int, float] = {}
    for t in txns:
        if t.security_id is None or not _in_window(t.posted_date, start_iso, end_iso):
            continue
        amt = income_for_txn(t, multipliers, include_reinvested)
        if amt:
            out[t.security_id] = out.get(t.security_id, 0.0) + amt
    return out


def income_by_month(
    txns, start_iso: str, end_iso: str,
    multipliers: dict[int, float], include_reinvested: bool,
) -> dict[str, float]:
    """``'YYYY-MM' → total income`` over ``[start_iso, end_iso]`` (native ccy).
    The month key is the ISO date's ``YYYY-MM`` prefix (calendar months)."""
    out: dict[str, float] = {}
    for t in txns:
        if not _in_window(t.posted_date, start_iso, end_iso):
            continue
        amt = income_for_txn(t, multipliers, include_reinvested)
        if amt:
            key = t.posted_date[:7]
            out[key] = out.get(key, 0.0) + amt
    return out


def enumerate_months(start_iso: str, end_iso: str) -> list[str]:
    """All ``'YYYY-MM'`` keys from ``start`` to ``end`` inclusive, so the chart
    renders empty months as zero bars rather than collapsing the axis."""
    sy, sm = int(start_iso[:4]), int(start_iso[5:7])
    ey, em = int(end_iso[:4]), int(end_iso[5:7])
    out: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out
