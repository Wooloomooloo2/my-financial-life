"""Pure compute for the Income & Expense report (ADR-064 / Arc E, E1).

No Qt, no SQL. The Repository hands in per-bucket income and expense
totals already FX-converted to one display currency (see
``Repository.income_expense_series``); this module:

- enumerates the full ordered bucket list for a date range + granularity
  so the chart has a continuous x-axis (empty buckets included);
- carries each bucket's income / expense / net;
- derives the summary metrics (totals, net saved, savings rate, averages).

Income and expense are both stored as **positive magnitudes** in major
units (pounds of the display currency); ``net`` is ``income - expense``
(signed). Mirrors the shape of :mod:`mfl_desktop.account_summary` and
:mod:`mfl_desktop.budget_calc` — pure, with all dates injected by the
caller so it's verifiable offscreen.

Income / expense are defined by **category kind** (income = inflows on
income-kind categories, expense = outflows on expense-kind categories,
transfers excluded) — the split is done in SQL by the Repository; this
module never sees the sign convention, only the two positive totals.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional


# SQL bucket-mode keys — must match Repository._BUCKET_EXPR keys and the
# strftime output they produce, so an enumerated key lines up with the
# aggregated key for the same period.
BUCKET_WEEK = "week"
BUCKET_MONTH = "month"
BUCKET_QUARTER = "quarter"
BUCKET_YEAR = "year"

_BUCKET_MODES = (BUCKET_WEEK, BUCKET_MONTH, BUCKET_QUARTER, BUCKET_YEAR)

_MONTHS = (
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


@dataclass(frozen=True)
class IEBucket:
    """One period column on the chart.

    ``key`` is the sortable bucket key (identical to the SQL strftime
    bucket); ``label`` is the display string. ``income`` and ``expense``
    are non-negative Decimals in the display currency's major units.
    """
    key: str
    label: str
    income: Decimal
    expense: Decimal

    @property
    def net(self) -> Decimal:
        return self.income - self.expense


@dataclass(frozen=True)
class IncomeExpenseSummary:
    """Headline figures for the right-hand summary panel."""
    total_income: Decimal
    total_expense: Decimal
    net: Decimal                       # total_income - total_expense
    savings_rate: Optional[float]      # net / total_income; None if income == 0
    avg_income: Decimal
    avg_expense: Decimal
    bucket_count: int


def _month_label(y: int, m: int) -> str:
    return f"{_MONTHS[m]} {y}"


def enumerate_buckets(
    date_from: date, date_to: date, mode: str,
) -> list[tuple[str, str]]:
    """Ordered ``(key, label)`` for every bucket the range touches —
    inclusive of the buckets ``date_from`` and ``date_to`` fall in — so
    the chart x-axis is continuous even where a bucket has no activity.

    Keys are byte-identical to the SQL ``strftime`` output in
    ``Repository._BUCKET_EXPR``: ``'2026'`` (year), ``'2026-Q1'``
    (quarter), ``'2026-01'`` (month), ``'2026-W05'`` (week). Year /
    quarter / month are calendar-aligned and enumerated analytically;
    weeks use ``strftime('%Y-W%W')`` per day because SQLite's ``%W``
    (Monday-first, week 00 = days before the year's first Monday) can
    split a single Mon–Sun span across two ``%Y`` years at the New Year
    boundary — iterating real dates keeps us identical to the aggregate.
    """
    if mode not in _BUCKET_MODES:
        raise ValueError(
            f"Unknown bucket mode {mode!r}; expected one of {_BUCKET_MODES}"
        )
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    out: list[tuple[str, str]] = []
    if mode == BUCKET_YEAR:
        for y in range(date_from.year, date_to.year + 1):
            out.append((f"{y:04d}", str(y)))
    elif mode == BUCKET_QUARTER:
        y, q = date_from.year, (date_from.month - 1) // 3 + 1
        end_y, end_q = date_to.year, (date_to.month - 1) // 3 + 1
        while (y, q) <= (end_y, end_q):
            out.append((f"{y:04d}-Q{q}", f"Q{q} {y}"))
            q += 1
            if q > 4:
                q, y = 1, y + 1
    elif mode == BUCKET_MONTH:
        y, m = date_from.year, date_from.month
        while (y, m) <= (date_to.year, date_to.month):
            out.append((f"{y:04d}-{m:02d}", _month_label(y, m)))
            m += 1
            if m > 12:
                m, y = 1, y + 1
    else:  # BUCKET_WEEK — walk real dates, dedupe keys in first-seen order.
        seen: set[str] = set()
        cur = date_from
        one_day = timedelta(days=1)
        while cur <= date_to:
            key = cur.strftime("%Y-W%W")
            if key not in seen:
                seen.add(key)
                monday = cur - timedelta(days=cur.weekday())
                out.append((key, f"{monday.day} {_MONTHS[monday.month]}"))
            cur = cur + one_day
    return out


def build_buckets(
    bucket_order: list[tuple[str, str]],
    income_pence: dict[str, int],
    expense_pence: dict[str, int],
) -> list[IEBucket]:
    """Zip the enumerated ``(key, label)`` order against the per-bucket
    income / expense pence maps from the Repository, filling missing
    buckets with zero. Pence (display-currency minor units) are converted
    to major-unit Decimals here, the one place the /100 happens."""
    hundred = Decimal(100)
    out: list[IEBucket] = []
    for key, label in bucket_order:
        inc = Decimal(income_pence.get(key, 0)) / hundred
        exp = Decimal(expense_pence.get(key, 0)) / hundred
        out.append(IEBucket(key=key, label=label, income=inc, expense=exp))
    return out


def compute_summary(buckets: list[IEBucket]) -> IncomeExpenseSummary:
    """Totals, net saved, savings rate and per-bucket averages over the
    full bucket list (empty buckets included in the average denominator —
    a month with no spend is still a month that pulls the mean down)."""
    total_income = sum((b.income for b in buckets), Decimal(0))
    total_expense = sum((b.expense for b in buckets), Decimal(0))
    net = total_income - total_expense
    n = len(buckets)
    avg_income = (total_income / n) if n else Decimal(0)
    avg_expense = (total_expense / n) if n else Decimal(0)
    savings_rate: Optional[float] = (
        float(net / total_income) if total_income > 0 else None
    )
    return IncomeExpenseSummary(
        total_income=total_income,
        total_expense=total_expense,
        net=net,
        savings_rate=savings_rate,
        avg_income=avg_income,
        avg_expense=avg_expense,
        bucket_count=n,
    )
