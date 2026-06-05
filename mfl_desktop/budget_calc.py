"""Budget computation — pure Python, no Qt, no SQL.

The Repository provides raw inputs (perimeter txns, budget categories,
category parent map, cash on hand); this module turns those into:

- a :class:`BudgetSummary` with the four Simplifi-style top-strip tiles
  (Income after bills & saving / Planned spending / Other spending /
  Available);
- a list of :class:`BudgetCardData` — one card per budgeted category,
  with the period-scoped budget, the actual spend bucketed against it,
  remaining, transaction count, and the cadence label.

Bucket assignment rule (per ADR-024): each in-perimeter transaction is
bucketed against the **nearest budgeted ancestor** of its category. If
no ancestor (including itself) is in the budget, the transaction lands
in the "Other" bucket — which the screen surfaces as the Other Spending
tile.

Pro-ration (ADR-024 § display cadence): the screen's period is one
calendar month. Per-category budgets are stored at their native cadence
and pro-rated to the month for the period-scoped numbers via the
average-month-length conversion. Matching-cadence (monthly on a
monthly screen) is a pass-through so the typical case shows the
user-entered figure unchanged.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from mfl_desktop.db.repository import (
    BudgetCategoryRow,
    PerimeterTxn,
)


# "Other" sentinel for the bucket assignment — distinct from any real
# category id (which are positive ints). Used as a dict key.
OTHER_BUCKET = -1


@dataclass(frozen=True)
class BudgetSummary:
    """The four top-of-screen tiles plus the cash-on-hand reality check."""
    income_after_bills_and_saving: Decimal
    planned_spending: Decimal              # discretionary expense budgets
    other_spending: Decimal                # actual on un-budgeted categories
    available: Decimal                     # income tile - planned - other
    # Components, useful for tooltips / hover detail.
    planned_income: Decimal
    planned_bills: Decimal
    planned_saving: Decimal
    cash_on_hand: Decimal


@dataclass(frozen=True)
class BudgetCardData:
    """One card on the budget screen, one per budgeted category.

    The period_* fields are scoped to the screen's calendar month. The
    cadence_period_* fields are scoped to the full calendar period at
    the card's native cadence (containing the reference date) — used by
    non-monthly cards' subtitle to show "this year: £X of £Y" etc.
    """
    category_id: int
    label: str               # 'Groceries (Food)' or 'Groceries' if top-level
    kind: str                # income / expense / transfer (from category)
    role: str                # bills / saving / discretionary
    cadence: str
    period_budget: Decimal   # pro-rated to the screen period (calendar month)
    period_actual: Decimal   # positive magnitude of spend bucketed here
    period_left: Decimal     # period_budget - period_actual (negative = over)
    period_txn_count: int
    cadence_amount: Decimal  # raw budget at its native cadence
    cadence_period_actual: Decimal   # actuals over the full cadence period
    cadence_period_label: str        # 'this week' / 'this year' / etc.
    scheduled_due_in_period: Decimal # sum of unposted scheduled outflows
                                     # due in the screen period, bucketed
                                     # against this card's category


@dataclass(frozen=True)
class BurnDownData:
    """Cumulative outflow series for the budget screen's burn-down chart.

    ``x_days`` is 1-indexed days of the period (1..period_days). ``actual``
    is the cumulative outflow magnitude per day; ``ideal`` is the linear
    pacing line — total planned outflow scaled by day-fraction-of-period.
    """
    x_days: list[int]
    actual: list[Decimal]
    ideal: list[Decimal]
    total_planned: Decimal
    today_day: int           # 1-indexed day-of-period for "today" marker;
                             # 0 if today is before the period start;
                             # period_days if today is after the period end.


@dataclass(frozen=True)
class SummaryBreakdown:
    """Segments for the proportional summary bar — five buckets that sum
    to planned income (or to total visible outflow when planned income
    is zero, so the bar stays drawable even before income is budgeted).
    """
    planned_income: Decimal
    bills: Decimal
    saving: Decimal
    planned_spending: Decimal
    other_spending: Decimal
    available: Decimal


# ── Period helpers ─────────────────────────────────────────────────────────


def calendar_month_period(year: int, month: int) -> tuple[str, str]:
    """ISO start (1st) and end (last day) of the given calendar month."""
    last_day = calendar.monthrange(year, month)[1]
    return (
        date(year, month, 1).isoformat(),
        date(year, month, last_day).isoformat(),
    )


# Average calendar lengths used for cadence pro-ration. Decimal so the
# whole computation stays exact-by-design — Decimal is consistent with the
# pence-based money handling elsewhere.
_DAYS_PER_MONTH = Decimal("30.4375")     # 365.25 / 12
_DAYS_PER_QUARTER = Decimal("91.3125")   # 365.25 / 4
_DAYS_PER_YEAR = Decimal("365.25")


def pro_rate_to_period(
    amount: Decimal,
    cadence: str,
    period_start: str,
    period_end: str,
) -> Decimal:
    """Convert an amount at its native cadence into the equivalent for the
    given period.

    The screen period is currently always one calendar month; matching
    cadences pass through unchanged so a £600/month budget shown on a
    monthly screen reads as £600, not £600 × (31 / 30.4375).
    """
    period_days = (
        date.fromisoformat(period_end) - date.fromisoformat(period_start)
    ).days + 1
    period_days_dec = Decimal(period_days)

    if cadence == "monthly":
        # Single calendar month → identity; partial-month periods (a future
        # period picker) would scale by period_days / 30.4375.
        if _is_single_calendar_month(period_start, period_end):
            return amount
        return _round2(amount * period_days_dec / _DAYS_PER_MONTH)
    if cadence == "weekly":
        return _round2(amount * period_days_dec / Decimal(7))
    if cadence == "biweekly":
        return _round2(amount * period_days_dec / Decimal(14))
    if cadence == "quarterly":
        return _round2(amount * period_days_dec / _DAYS_PER_QUARTER)
    if cadence == "annual":
        return _round2(amount * period_days_dec / _DAYS_PER_YEAR)
    raise ValueError(f"Unknown cadence: {cadence!r}")


def cadence_period_containing(
    cadence: str, ref_date: str,
) -> tuple[str, str, str]:
    """Calendar period at the given cadence that contains ``ref_date``.

    Returns ``(start_iso, end_iso, label)``. The label is the form used in
    card subtitles — "this week", "this fortnight", "this month",
    "this quarter", "this year". The global anchor rule (ADR-023) holds:
    weeks start Monday, quarters and years are calendar.

    Bi-weekly is genuinely ambiguous without per-schedule anchors. v1
    treats it as the 14-day window ending on ``ref_date`` so the bar
    answers the natural "what has hit the budget in the last fortnight"
    question; future per-schedule biweekly anchors would refine this.
    """
    d = date.fromisoformat(ref_date)
    if cadence == "weekly":
        monday = d - timedelta(days=d.weekday())
        sunday = monday + timedelta(days=6)
        return monday.isoformat(), sunday.isoformat(), "this week"
    if cadence == "biweekly":
        start = d - timedelta(days=13)
        return start.isoformat(), d.isoformat(), "this fortnight"
    if cadence == "monthly":
        last = calendar.monthrange(d.year, d.month)[1]
        return (
            date(d.year, d.month, 1).isoformat(),
            date(d.year, d.month, last).isoformat(),
            "this month",
        )
    if cadence == "quarterly":
        q = (d.month - 1) // 3
        start_month = q * 3 + 1
        end_month = start_month + 2
        last = calendar.monthrange(d.year, end_month)[1]
        return (
            date(d.year, start_month, 1).isoformat(),
            date(d.year, end_month, last).isoformat(),
            "this quarter",
        )
    if cadence == "annual":
        return (
            date(d.year, 1, 1).isoformat(),
            date(d.year, 12, 31).isoformat(),
            "this year",
        )
    raise ValueError(f"Unknown cadence: {cadence!r}")


def _is_single_calendar_month(period_start: str, period_end: str) -> bool:
    start = date.fromisoformat(period_start)
    end = date.fromisoformat(period_end)
    last = calendar.monthrange(start.year, start.month)[1]
    return (
        start.day == 1
        and end.year == start.year
        and end.month == start.month
        and end.day == last
    )


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Bucket assignment ──────────────────────────────────────────────────────


def nearest_budgeted_ancestor(
    category_id: int,
    parent_map: dict[int, Optional[int]],
    budgeted_ids: set[int],
) -> Optional[int]:
    """Walk up the parent chain; return the first ancestor id (including
    the category itself) that's in ``budgeted_ids``. Returns None when
    the chain reaches the root with nothing budgeted along the way."""
    current: Optional[int] = category_id
    seen: set[int] = set()
    while current is not None and current not in seen:
        if current in budgeted_ids:
            return current
        seen.add(current)
        current = parent_map.get(current)
    return None


# ── Main entry point ───────────────────────────────────────────────────────


def compute_budget_view(
    *,
    budget_categories: list[BudgetCategoryRow],
    perimeter_txns: list[PerimeterTxn],
    parent_map: dict[int, Optional[int]],
    cash_on_hand: Decimal,
    period_start: str,
    period_end: str,
    cadence_period_actuals_by_category: Optional[dict[int, Decimal]] = None,
    cadence_period_label_by_category: Optional[dict[int, str]] = None,
    scheduled_due_by_category: Optional[dict[int, Decimal]] = None,
) -> tuple[BudgetSummary, list[BudgetCardData]]:
    """Compose the summary tiles and the per-category cards in one pass.

    Pure function — easy to unit-test by passing fixtures in directly.

    Round-C optional inputs (all default to empty dicts):

    - ``cadence_period_actuals_by_category`` — per-card actual over the
      full calendar period at the card's native cadence (week / month /
      quarter / year containing the reference date). When the card's
      cadence is monthly and the screen period is one calendar month,
      the cadence value matches the period value and the subtitle is
      suppressed in the UI layer.
    - ``cadence_period_label_by_category`` — human label for the above
      ("this week", "this year", …).
    - ``scheduled_due_by_category`` — sum of unposted scheduled outflows
      whose ``next_due_date`` falls inside the screen period, bucketed
      against this card's category by the same nearest-budgeted-ancestor
      rule. Rendered on the card as an "+£X expected" badge.
    """
    cadence_actuals = cadence_period_actuals_by_category or {}
    cadence_labels = cadence_period_label_by_category or {}
    scheduled_due = scheduled_due_by_category or {}
    budgeted_ids = {bc.category_id for bc in budget_categories}
    bc_by_id = {bc.category_id: bc for bc in budget_categories}

    # ── 1. Bucket the perimeter txns ──
    # buckets[bucket_id] = (total_signed_pence_as_decimal, txn_count)
    buckets: dict[int, tuple[Decimal, int]] = {}
    for txn in perimeter_txns:
        bucket = nearest_budgeted_ancestor(
            txn.category_id, parent_map, budgeted_ids,
        )
        key = bucket if bucket is not None else OTHER_BUCKET
        prev_amount, prev_count = buckets.get(key, (Decimal("0"), 0))
        buckets[key] = (prev_amount + txn.amount, prev_count + 1)

    # ── 2. Per-card numbers ──
    cards: list[BudgetCardData] = []
    for bc in budget_categories:
        bucket_amount, bucket_count = buckets.get(
            bc.category_id, (Decimal("0"), 0),
        )
        # Actual: display as positive magnitude regardless of category kind.
        # Income card "actual" = sum of positive inflows; expense card
        # "actual" = absolute value of negative outflows. Either way the
        # number on the card means "how much real money moved through".
        period_actual = _round2(abs(bucket_amount))
        period_budget = pro_rate_to_period(
            bc.amount, bc.cadence, period_start, period_end,
        )
        period_left = _round2(period_budget - period_actual)
        label = (
            f"{bc.category_name} ({bc.category_parent_name})"
            if bc.category_parent_name else bc.category_name
        )
        cards.append(BudgetCardData(
            category_id=bc.category_id,
            label=label,
            kind=bc.category_kind,
            role=bc.role,
            cadence=bc.cadence,
            period_budget=period_budget,
            period_actual=period_actual,
            period_left=period_left,
            period_txn_count=bucket_count,
            cadence_amount=bc.amount,
            cadence_period_actual=_round2(
                cadence_actuals.get(bc.category_id, Decimal("0"))
            ),
            cadence_period_label=cadence_labels.get(bc.category_id, ""),
            scheduled_due_in_period=_round2(
                scheduled_due.get(bc.category_id, Decimal("0"))
            ),
        ))

    # ── 3. Top-strip tiles ──
    planned_income = Decimal("0.00")
    planned_bills = Decimal("0.00")
    planned_saving = Decimal("0.00")
    planned_spending = Decimal("0.00")
    for bc in budget_categories:
        # Transfer-kind categories are excluded from the tile math entirely
        # (ADR-024 §transfers). Per the spec, an intra-perimeter transfer
        # has zero effect on the budget — both planned and actual. The
        # actuals are already cancelled by the perimeter-txn filter at the
        # SQL layer, so the only remaining leak was here in the planned
        # loop, where a transfer-kind budget row would still subtract from
        # Income via planned_bills / planned_saving / planned_spending and
        # quietly reduce Available without any visible card actual to
        # explain it. The card is still rendered (so users can see their
        # planned transfer amount alongside the £0 actual that confirms
        # the cancellation worked) — it just doesn't move the tiles.
        # External transfers (one half outside the perimeter) are also
        # skipped here for the same reason; the in-perimeter half is
        # picked up as a card actual via the standard bucketing rule.
        if bc.category_kind == "transfer":
            continue
        period_amt = pro_rate_to_period(
            bc.amount, bc.cadence, period_start, period_end,
        )
        if bc.category_kind == "income":
            planned_income += period_amt
        elif bc.role == "bills":
            planned_bills += period_amt
        elif bc.role == "saving":
            planned_saving += period_amt
        else:
            planned_spending += period_amt

    other_bucket_amount, _ = buckets.get(OTHER_BUCKET, (Decimal("0"), 0))
    # "Other spending" is the magnitude of out-of-budget actual outflow
    # in the perimeter. Inflows to un-budgeted income categories would
    # technically subtract here — but in practice no user budgets income
    # then leaves it untracked, and the tile reads more cleanly as a
    # positive "you spent £X off-plan" number. We sum the negative-signed
    # outflows only.
    other_outflow = Decimal("0.00")
    for txn in perimeter_txns:
        bucket = nearest_budgeted_ancestor(
            txn.category_id, parent_map, budgeted_ids,
        )
        if bucket is None and txn.amount < 0:
            other_outflow += -txn.amount

    income_tile = _round2(planned_income - planned_bills - planned_saving)
    planned_spending = _round2(planned_spending)
    other_outflow = _round2(other_outflow)
    available = _round2(income_tile - planned_spending - other_outflow)

    summary = BudgetSummary(
        income_after_bills_and_saving=income_tile,
        planned_spending=planned_spending,
        other_spending=other_outflow,
        available=available,
        planned_income=_round2(planned_income),
        planned_bills=_round2(planned_bills),
        planned_saving=_round2(planned_saving),
        cash_on_hand=_round2(cash_on_hand),
    )
    return summary, cards


# ── Round C: burn-down chart + summary breakdown ───────────────────────────


def compute_burn_down(
    *,
    perimeter_txns: list[PerimeterTxn],
    summary: BudgetSummary,
    period_start: str,
    period_end: str,
    today: Optional[date] = None,
) -> BurnDownData:
    """Cumulative outflow per day vs. the linear "ideal" pacing line.

    The ideal line spreads the total planned outflow evenly across the
    period — at day d of an N-day period, ideal[d] = total_planned * d/N.
    Actuals are cumulative perimeter outflow magnitudes through day d.

    Total planned outflow = bills + saving + planned_spending. Income is
    excluded because the burn-down is a depletion chart, not a net-cash
    one. Transfers are already excluded from the planned tiles per the
    ADR-024 fix and are excluded here for the same reason.
    """
    today = today or date.today()
    start = date.fromisoformat(period_start)
    end = date.fromisoformat(period_end)
    period_days = (end - start).days + 1
    total_planned = (
        summary.planned_bills
        + summary.planned_saving
        + summary.planned_spending
    )

    # Pre-aggregate outflow magnitudes by day-of-period for one O(n) walk.
    by_day: dict[int, Decimal] = {}
    for txn in perimeter_txns:
        if txn.amount >= 0:
            continue
        d = date.fromisoformat(txn.posted_date)
        if d < start or d > end:
            continue
        day_idx = (d - start).days + 1   # 1-indexed
        by_day[day_idx] = by_day.get(day_idx, Decimal("0")) + (-txn.amount)

    x_days: list[int] = []
    actual: list[Decimal] = []
    ideal: list[Decimal] = []
    running = Decimal("0")
    for d in range(1, period_days + 1):
        running += by_day.get(d, Decimal("0"))
        x_days.append(d)
        actual.append(_round2(running))
        ideal.append(_round2(total_planned * Decimal(d) / Decimal(period_days)))

    if today < start:
        today_day = 0
    elif today > end:
        today_day = period_days
    else:
        today_day = (today - start).days + 1

    return BurnDownData(
        x_days=x_days,
        actual=actual,
        ideal=ideal,
        total_planned=_round2(total_planned),
        today_day=today_day,
    )


def compute_summary_breakdown(summary: BudgetSummary) -> SummaryBreakdown:
    """Five-segment breakdown for the proportional summary bar.

    Segments are positive magnitudes that sum to planned income:
    bills + saving + planned_spending + other_spending + available
    = planned_income (modulo rounding). When planned_income is zero
    (user hasn't budgeted income yet), the bar falls back to showing
    just the visible outflow segments — the proportional widget skips
    zero-amount segments so a zero income tile is a graceful empty.
    """
    return SummaryBreakdown(
        planned_income=summary.planned_income,
        bills=summary.planned_bills,
        saving=summary.planned_saving,
        planned_spending=summary.planned_spending,
        other_spending=summary.other_spending,
        available=summary.available,
    )
