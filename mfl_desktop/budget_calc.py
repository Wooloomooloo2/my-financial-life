"""Budget computation — pure Python, no Qt, no SQL (ADR-058).

The Repository provides raw inputs (budget lines, per-month allocations,
perimeter txns, the category parent/kind maps, the converted account pool);
this module turns them into a :class:`BudgetMatrix` — the 12-month grid that
backs every budget surface (annual matrix, monthly view, burn-down).

Per ADR-058 the matrix is the *native* object: each envelope (``budget_line``)
has an explicit per-month allocation, and **actuals + rollover are computed
here, never stored**. The core rules:

- **Bucketing (carried from ADR-024):** each in-perimeter txn is bucketed to
  the **nearest budgeted ancestor** of its category, per month. A txn with no
  budgeted ancestor is *unbudgeted* and lands in its section's synthetic
  "Unbudgeted" row (section chosen by the txn category's ``kind``).
- **Auto-rollover (ADR-058 D3):** for a line with ``rollover='accumulate'``,
  ``available = allocation + carry_in`` and the surplus/deficit
  (``available − actual``) carries to next month. ``rollover='none'`` resets
  each month (carry always 0). Carry runs both ways — an overspend reduces next
  month — clamping-at-zero is a deliberate non-feature for now.
- **Soft zero-sum (ADR-058 D2):** ``assigned`` per month = the sum of
  non-income allocations (money given a job to leave/save); the UI shows
  ``pool − assigned`` as an *Unallocated* indicator that reddens but never
  blocks.

Actuals are reported as **positive magnitudes** consistent with the line's
kind — an expense line's actual is its outflow magnitude, an income line's is
its inflow magnitude — so ``diff = available − actual`` reads naturally on the
matrix (positive = under budget / surplus, negative = over).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from mfl_desktop.db.repository import (
    Budget,
    BudgetLine,
    PerimeterTxn,
)


_ZERO = Decimal("0.00")

# Section ordering + titles for the three category kinds.
_SECTION_ORDER = ("income", "expense", "transfer")
_SECTION_TITLE = {
    "income": "Income",
    "expense": "Expenses",
    "transfer": "Transfers",
}


@dataclass(frozen=True)
class MonthCell:
    """One (line × month) cell of the matrix. ``available = allocation +
    carry_in``; ``diff = available − actual`` (positive = under/surplus)."""
    month: str           # 'YYYY-MM'
    allocation: Decimal  # budgeted this month (positive magnitude)
    actual: Decimal      # actual magnitude bucketed here this month
    carry_in: Decimal    # rollover brought in from prior months (0 if none)
    available: Decimal   # allocation + carry_in
    diff: Decimal        # available - actual


@dataclass(frozen=True)
class MatrixRow:
    """One row of the matrix — a budgeted envelope, or a section's synthetic
    'Unbudgeted' row (``is_unbudgeted=True``, ``line_id``/``category_id`` None).
    """
    line_id: Optional[int]
    category_id: Optional[int]
    label: str
    kind: str            # income / expense / transfer
    role: str            # bills / saving / discretionary ('' for unbudgeted)
    rollover: str        # none / accumulate ('' for unbudgeted)
    is_unbudgeted: bool
    cells: list[MonthCell]
    alloc_total: Decimal
    actual_total: Decimal


@dataclass(frozen=True)
class MatrixSection:
    """A kind-grouped block of the matrix (Income / Expenses / Transfers),
    with its budgeted rows (+ an Unbudgeted row when there's off-plan activity)
    and a per-month subtotal."""
    kind: str
    title: str
    rows: list[MatrixRow]
    subtotal: list[MonthCell]
    alloc_total: Decimal
    actual_total: Decimal


@dataclass(frozen=True)
class BudgetMatrix:
    """The whole 12-month budget grid (ADR-058) — the single source of budget
    truth that every surface reads from."""
    months: list[str]
    sections: list[MatrixSection]
    display_ccy: str
    pool: Decimal                       # converted perimeter balance (D2)
    excluded_accounts: list[str]        # accounts with no FX rate (D2 banner)
    assigned_by_month: list[Decimal]    # non-income allocations, per month
    today_month: Optional[str] = None   # 'YYYY-MM' in range, or None


# ── Bucket assignment (carried from ADR-024) ───────────────────────────────


def nearest_budgeted_ancestor(
    category_id: int,
    parent_map: dict[int, Optional[int]],
    budgeted_ids: set[int],
) -> Optional[int]:
    """Walk up the parent chain; return the first ancestor id (including the
    category itself) that's in ``budgeted_ids``. Returns None when the chain
    reaches the root with nothing budgeted along the way."""
    current: Optional[int] = category_id
    seen: set[int] = set()
    while current is not None and current not in seen:
        if current in budgeted_ids:
            return current
        seen.add(current)
        current = parent_map.get(current)
    return None


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _favourable_diff(kind: str, available: Decimal, actual: Decimal) -> Decimal:
    """Signed so positive is always 'good' (green): income (and a goal, where
    paying ≥ the required amount is good) is favourable when actual ≥ budget; an
    expense/transfer when actual ≤ budget."""
    if kind in ("income", "goals"):
        return _round2(actual - available)
    return _round2(available - actual)


@dataclass(frozen=True)
class GoalPlan:
    """A goal's per-month planned + actual figures, prepared by the window from
    ``goal_calc`` (required monthly, spread over the months to target) and the
    account's payment inflows. Rendered into the matrix's Goals section (R4b)."""
    goal_id: int
    label: str
    planned: dict        # 'YYYY-MM' -> Decimal required payment that month
    actual: dict         # 'YYYY-MM' -> Decimal actually paid that month


# ── Main entry point ───────────────────────────────────────────────────────


def compute_matrix(
    *,
    budget: Budget,
    lines: list[BudgetLine],
    allocations: dict[tuple[int, str], Decimal],
    perimeter_txns: list[PerimeterTxn],
    parent_map: dict[int, Optional[int]],
    kind_map: dict[int, str],
    pool: Decimal = _ZERO,
    excluded_accounts: Optional[list[str]] = None,
    display_ccy: str = "",
    today_month: Optional[str] = None,
    goal_plans: Optional[list["GoalPlan"]] = None,
) -> BudgetMatrix:
    """Assemble the budget matrix. Pure function — fixture-friendly.

    ``allocations`` is keyed ``(budget_line_id, 'YYYY-MM')``; absent cells are
    treated as 0. ``perimeter_txns`` should span the budget's full month range
    (the caller queries ``list_perimeter_txns`` over month[0]..month[-1]).
    ``goal_plans`` (R4b) add a Goals section and count their planned amounts in
    ``assigned`` (a pay-down/savings goal is money given a job — zero-sum).
    """
    months = budget.months()
    month_index = {m: i for i, m in enumerate(months)}
    n = len(months)
    budgeted_ids = {ln.category_id for ln in lines}

    # ── 1. Bucket actuals by (bucket, month). Signed sum, magnitude at use. ──
    # bucket key: a budgeted category id, or None for unbudgeted.
    actual_signed: dict[tuple[Optional[int], int], Decimal] = {}
    for txn in perimeter_txns:
        mi = month_index.get(txn.posted_date[:7])
        if mi is None:
            continue  # outside the budget window
        bucket = nearest_budgeted_ancestor(
            txn.category_id, parent_map, budgeted_ids,
        )
        key = (bucket, mi)
        actual_signed[key] = actual_signed.get(key, _ZERO) + txn.amount

    def magnitude(bucket: Optional[int], mi: int) -> Decimal:
        return abs(actual_signed.get((bucket, mi), _ZERO))

    # ── 2. Budgeted rows, grouped by kind, with rollover folded forward. ──
    rows_by_kind: dict[str, list[MatrixRow]] = {k: [] for k in _SECTION_ORDER}
    assigned_by_month = [_ZERO for _ in range(n)]

    for ln in lines:
        cells: list[MonthCell] = []
        carry_in = _ZERO
        alloc_total = _ZERO
        actual_total = _ZERO
        for mi, m in enumerate(months):
            alloc = allocations.get((ln.id, m), _ZERO)
            actual = _round2(magnitude(ln.category_id, mi))
            available = _round2(alloc + carry_in)
            # Diff is signed so **positive is always favourable** (green): an
            # expense is favourable when you spend under budget (available −
            # actual); income is favourable when you earn over plan (actual −
            # available). The raw surplus (available − actual) is what carries
            # forward for rollover, regardless of how diff is displayed.
            raw_surplus = _round2(available - actual)
            diff = _favourable_diff(ln.category_kind, available, actual)
            cells.append(MonthCell(
                month=m, allocation=_round2(alloc), actual=actual,
                carry_in=_round2(carry_in), available=available, diff=diff,
            ))
            alloc_total += alloc
            actual_total += actual
            if ln.category_kind != "income":
                assigned_by_month[mi] += alloc
            # Carry the surplus/deficit forward only for accumulate lines.
            carry_in = raw_surplus if ln.rollover == "accumulate" else _ZERO
        label = (
            f"{ln.category_name} ({ln.category_parent_name})"
            if ln.category_parent_name else ln.category_name
        )
        rows_by_kind.setdefault(ln.category_kind, []).append(MatrixRow(
            line_id=ln.id, category_id=ln.category_id, label=label,
            kind=ln.category_kind, role=ln.role, rollover=ln.rollover,
            is_unbudgeted=False, cells=cells,
            alloc_total=_round2(alloc_total), actual_total=_round2(actual_total),
        ))

    # ── 3. Unbudgeted rows — one per section, only if there's activity. ──
    # The signed-sum buckets lost the per-txn kind, so re-walk perimeter txns to
    # split the un-bucketed ones by their category kind into the right section.
    unbudg: dict[str, list[Decimal]] = {
        k: [_ZERO for _ in range(n)] for k in _SECTION_ORDER
    }
    for txn in perimeter_txns:
        mi = month_index.get(txn.posted_date[:7])
        if mi is None:
            continue
        if nearest_budgeted_ancestor(
            txn.category_id, parent_map, budgeted_ids,
        ) is not None:
            continue
        section_kind = kind_map.get(txn.category_id, "expense")
        if section_kind not in unbudg:
            section_kind = "expense"
        unbudg[section_kind][mi] += txn.amount

    for kind in _SECTION_ORDER:
        monthly = unbudg[kind]
        if not any(v != 0 for v in monthly):
            continue
        cells = []
        alloc_total = _ZERO
        actual_total = _ZERO
        for mi, m in enumerate(months):
            actual = _round2(abs(monthly[mi]))
            cells.append(MonthCell(
                month=m, allocation=_ZERO, actual=actual, carry_in=_ZERO,
                available=_ZERO, diff=_favourable_diff(kind, _ZERO, actual),
            ))
            actual_total += actual
        rows_by_kind[kind].append(MatrixRow(
            line_id=None, category_id=None, label="Unbudgeted",
            kind=kind, role="", rollover="", is_unbudgeted=True, cells=cells,
            alloc_total=_ZERO, actual_total=_round2(actual_total),
        ))

    # ── 4. Sections with per-month subtotals. ──
    sections: list[MatrixSection] = []
    for kind in _SECTION_ORDER:
        rows = rows_by_kind[kind]
        if not rows:
            continue
        subtotal: list[MonthCell] = []
        sec_alloc_total = _ZERO
        sec_actual_total = _ZERO
        for mi, m in enumerate(months):
            alloc = sum((r.cells[mi].allocation for r in rows), _ZERO)
            actual = sum((r.cells[mi].actual for r in rows), _ZERO)
            available = sum((r.cells[mi].available for r in rows), _ZERO)
            subtotal.append(MonthCell(
                month=m, allocation=_round2(alloc), actual=_round2(actual),
                carry_in=_ZERO, available=_round2(available),
                diff=_favourable_diff(kind, available, actual),
            ))
            sec_alloc_total += alloc
            sec_actual_total += actual
        sections.append(MatrixSection(
            kind=kind, title=_SECTION_TITLE[kind], rows=rows,
            subtotal=subtotal,
            alloc_total=_round2(sec_alloc_total),
            actual_total=_round2(sec_actual_total),
        ))

    # ── 5. Goals section (R4b) — planned required payment vs actual paid. ──
    # A goal's planned amount is money given a job, so it adds to `assigned`
    # (the Unallocated indicator drops by it). The actual payment is a transfer
    # that cancels in the perimeter, so it never double-counts as spending.
    if goal_plans:
        goal_rows: list[MatrixRow] = []
        for gp in goal_plans:
            cells = []
            g_alloc_total = _ZERO
            g_actual_total = _ZERO
            for mi, m in enumerate(months):
                alloc = _round2(gp.planned.get(m, _ZERO))
                actual = _round2(gp.actual.get(m, _ZERO))
                cells.append(MonthCell(
                    month=m, allocation=alloc, actual=actual, carry_in=_ZERO,
                    available=alloc,
                    diff=_favourable_diff("goals", alloc, actual),
                ))
                g_alloc_total += alloc
                g_actual_total += actual
                assigned_by_month[mi] += alloc
            goal_rows.append(MatrixRow(
                line_id=gp.goal_id, category_id=None, label=gp.label,
                kind="goals", role="", rollover="", is_unbudgeted=False,
                cells=cells, alloc_total=_round2(g_alloc_total),
                actual_total=_round2(g_actual_total),
            ))
        gsub: list[MonthCell] = []
        gs_alloc = _ZERO
        gs_actual = _ZERO
        for mi, m in enumerate(months):
            alloc = sum((r.cells[mi].allocation for r in goal_rows), _ZERO)
            actual = sum((r.cells[mi].actual for r in goal_rows), _ZERO)
            gsub.append(MonthCell(
                month=m, allocation=_round2(alloc), actual=_round2(actual),
                carry_in=_ZERO, available=_round2(alloc),
                diff=_favourable_diff("goals", alloc, actual),
            ))
            gs_alloc += alloc
            gs_actual += actual
        sections.append(MatrixSection(
            kind="goals", title="Goals", rows=goal_rows, subtotal=gsub,
            alloc_total=_round2(gs_alloc), actual_total=_round2(gs_actual),
        ))

    return BudgetMatrix(
        months=months,
        sections=sections,
        display_ccy=display_ccy,
        pool=_round2(pool),
        excluded_accounts=excluded_accounts or [],
        assigned_by_month=[_round2(v) for v in assigned_by_month],
        today_month=today_month if today_month in month_index else None,
    )


# ── Burn-down (ADR-058 R3, principle 12) ───────────────────────────────────


@dataclass(frozen=True)
class BurnDownData:
    """One month's spend depletion for the burn-down chart (ADR-058 R3).

    Days are 1-indexed into the focused month. Three series share the same
    day x-axis but cover different spans:

    - **actual** — cumulative outflow magnitude through *today* (so an
      in-progress month stops at today; a finished month runs the full span).
    - **ideal** — the linear pacing line, spreading ``total_planned`` evenly
      across every day of the month.
    - **proj** — the *projection* that makes this better than Pocketsmith
      (principle 12): from today to month-end it extends the observed average
      daily rate, so an overspending line keeps climbing (and crosses the
      budget early) instead of going flat at today.

    ``total_planned`` is the scope's *available* for the month (allocation +
    rolled-in carry); ``scope_label`` names the scope ("Whole budget" or a
    category). All amounts are positive magnitudes (a depletion chart).
    """
    month: str                 # 'YYYY-MM'
    scope_label: str
    period_days: int
    today_day: int             # 0 before the month, period_days after it
    total_planned: Decimal
    x_days: list[int]          # 1..period_days — the axis
    actual_x: list[int]
    actual: list[Decimal]
    ideal_x: list[int]
    ideal: list[Decimal]
    proj_x: list[int]
    proj: list[Decimal]
    projected_end: Decimal     # where the projection lands at month-end


def _month_bounds(month: str) -> tuple[date, date, int]:
    """(first, last, day_count) for a 'YYYY-MM' month."""
    y, m = int(month[:4]), int(month[5:7])
    start = date(y, m, 1)
    end = (date(y, 12, 31) if m == 12
           else date(y, m + 1, 1) - timedelta(days=1))
    return start, end, (end - start).days + 1


def compute_burndown(
    *,
    perimeter_txns: list[PerimeterTxn],
    month: str,
    total_planned: Decimal,
    today: Optional[date] = None,
    scope_label: str = "Whole budget",
    target_category_id: Optional[int] = None,
    parent_map: Optional[dict[int, Optional[int]]] = None,
    budgeted_ids: Optional[set[int]] = None,
    kind_map: Optional[dict[int, str]] = None,
) -> BurnDownData:
    """Build the burn-down series for one month and one scope (pure).

    ``perimeter_txns`` may span more than the month — they're filtered here.
    With ``target_category_id`` the scope is a single envelope: outflows are
    kept when their **nearest budgeted ancestor** is that category (so the
    series reconciles with the matrix's Actual cell). Without it the scope is
    the whole budget: every **expense-kind** outflow counts (income inflows
    and transfers are excluded — this is a spending-depletion chart).
    """
    today = today or date.today()
    parent_map = parent_map or {}
    budgeted_ids = budgeted_ids or set()
    kind_map = kind_map or {}
    start, end, period_days = _month_bounds(month)

    # Outflow magnitude by day-of-month for the chosen scope.
    by_day: dict[int, Decimal] = {}
    for txn in perimeter_txns:
        if txn.amount >= 0:               # depletion = outflows only
            continue
        if txn.posted_date[:7] != month:
            continue
        if target_category_id is None:
            if kind_map.get(txn.category_id, "expense") != "expense":
                continue
        else:
            bucket = nearest_budgeted_ancestor(
                txn.category_id, parent_map, budgeted_ids,
            )
            if bucket != target_category_id:
                continue
        day_idx = (date.fromisoformat(txn.posted_date) - start).days + 1
        if day_idx < 1 or day_idx > period_days:
            continue
        by_day[day_idx] = by_day.get(day_idx, _ZERO) + (-txn.amount)

    if today < start:
        today_day = 0
    elif today > end:
        today_day = period_days
    else:
        today_day = (today - start).days + 1

    x_days = list(range(1, period_days + 1))
    ideal = [
        _round2(total_planned * Decimal(d) / Decimal(period_days))
        for d in x_days
    ]

    # Actual cumulative outflow, only through today (no future actuals exist).
    actual_x: list[int] = []
    actual: list[Decimal] = []
    running = _ZERO
    for d in range(1, today_day + 1):
        running += by_day.get(d, _ZERO)
        actual_x.append(d)
        actual.append(_round2(running))

    # Projection: extend the observed average daily rate to month-end.
    proj_x: list[int] = []
    proj: list[Decimal] = []
    if 1 <= today_day < period_days and actual:
        spent = actual[-1]
        rate = spent / Decimal(today_day)
        for d in range(today_day, period_days + 1):
            proj_x.append(d)
            proj.append(_round2(spent + rate * Decimal(d - today_day)))

    projected_end = (
        proj[-1] if proj else (actual[-1] if actual else _ZERO)
    )

    return BurnDownData(
        month=month, scope_label=scope_label, period_days=period_days,
        today_day=today_day, total_planned=_round2(total_planned),
        x_days=x_days, actual_x=actual_x, actual=actual,
        ideal_x=x_days, ideal=ideal, proj_x=proj_x, proj=proj,
        projected_end=projected_end,
    )
