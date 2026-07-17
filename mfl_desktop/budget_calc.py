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

from dataclasses import dataclass, replace
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


# Row kinds within a section (ADR-170). A budgeted category whose subtree
# contains other budgeted categories renders as a *group*: a roll-up header,
# its budgeted children indented beneath, and — when it still holds money or
# spending of its own — a `residual` row carrying the parent's own line.
_LEAF = "leaf"              # a budgeted line with no budgeted descendants
_GROUP = "group"            # roll-up header: own residual + all descendants
_RESIDUAL = "residual"      # 'Everything else' — the group's own line
_UNBUDGETED = "unbudgeted"  # a section's synthetic off-plan row


@dataclass(frozen=True)
class MatrixRow:
    """One row of the matrix — a budgeted envelope, or a section's synthetic
    'Unbudgeted' row (``is_unbudgeted=True``, ``line_id``/``category_id`` None).

    ADR-170: rows carry their place in the category tree. ``depth`` is the
    indent level (0 = top of its section) and ``row_kind`` says what the row
    *is*:

    - ``leaf`` — a budgeted line with no budgeted descendants. Its cells are
      its own; it is editable. This is what every row was before ADR-170.
    - ``group`` — the roll-up header for a budgeted category that has budgeted
      descendants. Its cells are **the sum of its own residual and every
      budgeted descendant**, so a collapsed group still tells the whole truth.
      Not editable: it is a sum, not a stored allocation.
    - ``residual`` — 'Everything else': a group's *own* line, carrying the
      spending that no budgeted child claimed. Same ``line_id`` as its group
      header, and editable — this is where the parent's allocation actually
      lives. Emitted only when it holds a non-zero allocation or actual.
    - ``unbudgeted`` — the section's synthetic off-plan row.

    A ``group`` and its ``residual`` share a ``line_id`` and ``category_id`` by
    design: they are two views of one budget line — the whole and the remainder.
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
    # ADR-094: the linked schedule when this line is a bill (else None).
    scheduled_txn_id: Optional[int] = None
    # ADR-170: position in the category tree.
    depth: int = 0
    row_kind: str = _LEAF
    # The category's own parent name ('' if top-level), kept so a row nested
    # under an *unbudgeted* parent can still disambiguate itself — see
    # ``_labelled``.
    parent_name: str = ""

    @property
    def is_group(self) -> bool:
        """A roll-up header — collapsible, and never editable."""
        return self.row_kind == _GROUP

    @property
    def is_editable(self) -> bool:
        """Only a real stored allocation can be typed into: leaves and the
        residual line. A group header is a sum; Unbudgeted has no line."""
        return self.row_kind in (_LEAF, _RESIDUAL) and self.line_id is not None


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


def is_ancestor_or_self(
    ancestor_id: int,
    category_id: Optional[int],
    parent_map: dict[int, Optional[int]],
) -> bool:
    """True when ``category_id`` is ``ancestor_id`` or sits below it.

    The drill-down's counterpart to :func:`nearest_budgeted_ancestor`: a group
    row's roll-up covers every txn whose *bucket* is the group's category or a
    budgeted category beneath it, and this is the containment test (ADR-170).
    """
    current: Optional[int] = category_id
    seen: set[int] = set()
    while current is not None and current not in seen:
        if current == ancestor_id:
            return True
        seen.add(current)
        current = parent_map.get(current)
    return False


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


# ── Hierarchy (ADR-170) ────────────────────────────────────────────────────


def _rollup_cells(rows: list[MatrixRow], n: int, kind: str) -> list[MonthCell]:
    """Sum a group's own residual + every budgeted descendant, per month.

    Summing is sound precisely *because* bucketing sends each txn to exactly
    one budgeted ancestor: a parent line only ever holds what no budgeted child
    claimed, so parent + descendants double-counts nothing. This is the same
    reasoning that already made section subtotals correct.

    ``carry_in`` is summed for display (the group's rollover is the aggregate
    of its parts), but the group is never itself a rollover *line* — carry is
    computed and carried on the underlying leaves/residual, not here.
    """
    out: list[MonthCell] = []
    for mi in range(n):
        alloc = sum((r.cells[mi].allocation for r in rows), _ZERO)
        actual = sum((r.cells[mi].actual for r in rows), _ZERO)
        carry = sum((r.cells[mi].carry_in for r in rows), _ZERO)
        available = sum((r.cells[mi].available for r in rows), _ZERO)
        out.append(MonthCell(
            month=rows[0].cells[mi].month,
            allocation=_round2(alloc), actual=_round2(actual),
            carry_in=_round2(carry), available=_round2(available),
            diff=_favourable_diff(kind, available, actual),
        ))
    return out


def _labelled(row: MatrixRow, depth: int) -> str:
    """A row's display label for its depth (ADR-170).

    Indentation replaces the parenthetical: nested under Bills, a row reads
    'Cable and Internet', because its position already says '(Bills)'. But a
    line whose parent is *not* budgeted has no group to sit under and lands at
    depth 0 — there the suffix is the only thing distinguishing 'Insurance
    (Car)' from 'Insurance (Home)', so it stays.
    """
    if depth == 0 and row.parent_name:
        return f"{row.label} ({row.parent_name})"
    return row.label


def _arrange_hierarchy(
    rows: list[MatrixRow],
    parent_map: dict[int, Optional[int]],
    budgeted_ids: set[int],
    n: int,
) -> list[MatrixRow]:
    """Turn a section's flat budgeted rows into an indented, rolled-up tree.

    Each line's group parent is the nearest budgeted ancestor **of its
    category's parent** (excluding itself). A line with budgeted descendants
    becomes a ``group`` header whose cells roll its subtree up; the line's own
    figures move to a ``residual`` row ('Everything else') emitted only when
    they are non-zero — the anti-clutter rule. A line with no budgeted
    descendants stays a plain editable ``leaf``, exactly as before ADR-170.

    Returns a pre-order (depth-first) list ready to render top to bottom.
    """
    by_cat: dict[int, MatrixRow] = {
        r.category_id: r for r in rows
        if not r.is_unbudgeted and r.category_id is not None
    }
    children: dict[Optional[int], list[int]] = {}
    for cat in by_cat:
        parent = nearest_budgeted_ancestor(
            parent_map.get(cat), parent_map, budgeted_ids,
        ) if parent_map.get(cat) is not None else None
        # A budgeted ancestor outside this section (kinds shouldn't mix, but a
        # miscategorised tree could) is treated as top-level here.
        if parent is not None and parent not in by_cat:
            parent = None
        children.setdefault(parent, []).append(cat)

    def own_subtree(cat: int) -> list[MatrixRow]:
        """The *own* (unarranged) rows of ``cat`` and everything beneath it —
        one row per real budget line, each counted exactly once.

        The roll-up must be built from these, never from the arranged rows: an
        arranged subtree contains its own group headers *and* the descendants
        those headers already rolled up, so summing it double-counts every
        level below the first. Same trap as the section subtotal.
        """
        out = [by_cat[cat]]
        for kid in children.get(cat, []):
            out.extend(own_subtree(kid))
        return out

    def emit(cat: int, depth: int) -> list[MatrixRow]:
        own = by_cat[cat]
        kids = sorted(children.get(cat, []), key=lambda c: by_cat[c].label)
        if not kids:
            # No budgeted descendants — a plain leaf, untouched by ADR-170.
            return [_with(
                own, depth=depth, row_kind=_LEAF,
                label=_labelled(own, depth),
            )]

        arranged: list[MatrixRow] = []
        for kid in kids:
            arranged.extend(emit(kid, depth + 1))
        # 'Everything else' — the parent's own line, honestly labelled. Shown
        # only when it holds money or spending; itemise a group to the penny
        # and the row simply disappears.
        residual = None
        if own.alloc_total != 0 or own.actual_total != 0:
            residual = _with(
                own, depth=depth + 1, row_kind=_RESIDUAL,
                label="Everything else",
            )
        # `own_subtree` includes `own` itself, so a hidden (all-zero) residual
        # is still counted — the header never depends on the residual showing.
        flat = own_subtree(cat)
        header = _with(
            own, depth=depth, row_kind=_GROUP,
            label=_labelled(own, depth),
            cells=_rollup_cells(flat, n, own.kind),
            alloc_total=_round2(sum((r.alloc_total for r in flat), _ZERO)),
            actual_total=_round2(sum((r.actual_total for r in flat), _ZERO)),
        )
        # Children first, remainder last: 'Everything else' reads as what is
        # left over after the itemised lines, not as a peer among them.
        return [header] + arranged + ([residual] if residual else [])

    out: list[MatrixRow] = []
    for cat in sorted(children.get(None, []), key=lambda c: by_cat[c].label):
        out.extend(emit(cat, 0))
    # The synthetic Unbudgeted row (if any) always trails its section.
    out.extend(r for r in rows if r.is_unbudgeted)
    return out


def _with(row: MatrixRow, **changes) -> MatrixRow:
    """``dataclasses.replace`` for a frozen MatrixRow."""
    return replace(row, **changes)


# ── Collapse keys + filtering (ADR-170) ────────────────────────────────────
#
# Both budget surfaces (the annual matrix and the monthly view) render the same
# tree and share one collapse set, so the key vocabulary and the filter live
# here — beside the hierarchy they describe, and reachable from both without
# either view importing the other. Pure string/list work; still no Qt.


def section_key(kind: str) -> str:
    """The collapse key for a section. Keyed by *kind*, not by list index: an
    index shifts when a whole kind empties out, which would silently move a
    remembered collapse onto a different section."""
    return f"section:{kind}"


def group_key(category_id: int) -> str:
    """The collapse key for a group header — its category id."""
    return f"group:{category_id}"


def row_group_key(row: MatrixRow) -> Optional[str]:
    """``row``'s collapse key if it is a group header, else None."""
    if row.is_group and row.category_id is not None:
        return group_key(row.category_id)
    return None


def visible_rows(rows: list[MatrixRow], collapsed: set[str]) -> list[MatrixRow]:
    """``rows`` with every collapsed group's subtree dropped.

    A section's rows are pre-order with a ``depth`` each, so a collapsed
    group's descendants are exactly the rows following it until depth returns
    to the group's own level. Nested collapses need no special handling — an
    outer collapse swallows the inner one, and the inner key is simply
    remembered for when the outer reopens.
    """
    out: list[MatrixRow] = []
    hide_below: Optional[int] = None
    for row in rows:
        if hide_below is not None:
            if row.depth > hide_below:
                continue
            hide_below = None
        out.append(row)
        key = row_group_key(row)
        if key is not None and key in collapsed:
            hide_below = row.depth
    return out


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
        # The bare category name; `_arrange_hierarchy` re-adds the '(Parent)'
        # suffix only where indentation can't convey it (ADR-170).
        rows_by_kind.setdefault(ln.category_kind, []).append(MatrixRow(
            line_id=ln.id, category_id=ln.category_id, label=ln.category_name,
            kind=ln.category_kind, role=ln.role, rollover=ln.rollover,
            is_unbudgeted=False, cells=cells,
            alloc_total=_round2(alloc_total), actual_total=_round2(actual_total),
            scheduled_txn_id=getattr(ln, "scheduled_txn_id", None),
            parent_name=ln.category_parent_name,
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
    # The subtotal sums the **flat** rows — one row per real budget line —
    # *before* the hierarchy is arranged. Summing the arranged rows instead
    # would count every group header twice: once as the header's roll-up and
    # again as the descendants it rolled up (ADR-170).
    sections: list[MatrixSection] = []
    for kind in _SECTION_ORDER:
        flat = rows_by_kind[kind]
        if not flat:
            continue
        subtotal: list[MonthCell] = []
        sec_alloc_total = _ZERO
        sec_actual_total = _ZERO
        for mi, m in enumerate(months):
            alloc = sum((r.cells[mi].allocation for r in flat), _ZERO)
            actual = sum((r.cells[mi].actual for r in flat), _ZERO)
            available = sum((r.cells[mi].available for r in flat), _ZERO)
            subtotal.append(MonthCell(
                month=m, allocation=_round2(alloc), actual=_round2(actual),
                carry_in=_ZERO, available=_round2(available),
                diff=_favourable_diff(kind, available, actual),
            ))
            sec_alloc_total += alloc
            sec_actual_total += actual
        sections.append(MatrixSection(
            kind=kind, title=_SECTION_TITLE[kind],
            rows=_arrange_hierarchy(flat, parent_map, budgeted_ids, n),
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
    # ADR-172. The chart draws **remaining** (`total_planned − cumulative`),
    # so these are the two facts it leads with. They live here, not in the
    # paint code, because they are arithmetic over the series and belong where
    # the series is built — and where a test can reach them without a Qt event
    # loop. `projected_end` was already computed here and read by *nothing*:
    # the chart calculated the answer and made the reader eyeball it.
    projected_remaining: Decimal = _ZERO   # total_planned − projected_end
    # The first day the plan is exhausted (remaining ≤ 0) — from the actuals if
    # it has already happened, else from the projection. None = it doesn't,
    # this month. This is the reading a rising line cannot give you.
    runs_out_day: Optional[int] = None


def _month_bounds(month: str) -> tuple[date, date, int]:
    """(first, last, day_count) for a 'YYYY-MM' month."""
    y, m = int(month[:4]), int(month[5:7])
    start = date(y, m, 1)
    end = (date(y, 12, 31) if m == 12
           else date(y, m + 1, 1) - timedelta(days=1))
    return start, end, (end - start).days + 1


# ── Bills (ADR-094) ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BillSchedule:
    """A bill's recurrence, as seen by the budget engine (ADR-094) — the subset
    of a linked ``scheduled_txn`` needed to expand its occurrences. ``amount`` is
    a positive magnitude (the estimated outflow per occurrence)."""
    category_id: int
    cadence: str                 # weekly / biweekly / monthly / quarterly / annual
    anchor_date: str             # 'YYYY-MM-DD' — first occurrence
    amount: Decimal
    end_date: Optional[str] = None


@dataclass(frozen=True)
class BillOccurrence:
    """One expected occurrence of a bill within a month (ADR-094). ``day`` is
    1-indexed day-of-month; ``amount`` is the positive estimated magnitude."""
    category_id: int
    day: int
    amount: Decimal


def _occurrence_days(
    cadence: str, anchor_date: str, month: str, end_date: Optional[str],
) -> list[int]:
    """Day-of-month numbers for every occurrence of a schedule that lands in
    ``month`` ('YYYY-MM'), anchored at ``anchor_date`` (the first occurrence).
    Mirrors Repository.compute_next_due_date's anchor rule: weekly/biweekly step
    by 7/14 days; monthly/quarterly/annual recur every 1/3/12 months on the
    anchor day (clamped to the month length). Occurrences before the anchor or
    after ``end_date`` are excluded."""
    start, end, period_days = _month_bounds(month)
    try:
        anchor = date.fromisoformat(anchor_date)
    except ValueError:
        return []
    end_d = date.fromisoformat(end_date) if end_date else None
    days: list[int] = []
    if cadence in ("weekly", "biweekly"):
        step = 7 if cadence == "weekly" else 14
        d = anchor
        if d < start:
            # Jump forward to the first occurrence on/after the month start.
            n = (start - d).days // step
            d = anchor + timedelta(days=n * step)
            while d < start:
                d += timedelta(days=step)
        while d <= end:
            if d >= anchor and (end_d is None or d <= end_d):
                days.append(d.day)
            d += timedelta(days=step)
    elif cadence in ("monthly", "quarterly", "annual"):
        step = {"monthly": 1, "quarterly": 3, "annual": 12}[cadence]
        y, m = int(month[:4]), int(month[5:7])
        total = (y - anchor.year) * 12 + (m - anchor.month)
        if total >= 0 and total % step == 0:
            day = min(anchor.day, period_days)
            occ = date(y, m, day)
            if occ >= anchor and (end_d is None or occ <= end_d):
                days.append(day)
    return sorted(days)


def bill_occurrences_in_month(
    bills: list[BillSchedule], month: str,
) -> list[BillOccurrence]:
    """Expand every bill schedule into its occurrences within ``month`` (pure,
    ADR-094). A monthly bill yields one occurrence; a weekly bill yields ~4–5."""
    out: list[BillOccurrence] = []
    for b in bills:
        for day in _occurrence_days(b.cadence, b.anchor_date, month, b.end_date):
            out.append(BillOccurrence(
                category_id=b.category_id, day=day, amount=abs(b.amount),
            ))
    return out


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
    bill_occurrences: Optional[list[BillOccurrence]] = None,
) -> BurnDownData:
    """Build the burn-down series for one month and one scope (pure).

    ``perimeter_txns`` may span more than the month — they're filtered here.
    With ``target_category_id`` the scope is a single envelope: outflows are
    kept when their **nearest budgeted ancestor** is that category (so the
    series reconciles with the matrix's Actual cell). Without it the scope is
    the whole budget: every **expense-kind** outflow counts (income inflows
    and transfers are excluded — this is a spending-depletion chart).

    ``bill_occurrences`` (ADR-094) are the scope's scheduled-bill occurrences in
    the month (expanded via ``bill_occurrences_in_month``). When present the
    **ideal** becomes a staircase (each bill a step at its due day, the
    discretionary remainder spread linearly) and the **projection** = actual +
    the *unpaid* bill occurrences (amount-matched against actuals, an overdue
    one stepping at today) + the discretionary run-rate on non-bill spend only.
    A paid bill drops out, so its line goes flat. With no bills this is the old
    behaviour exactly (linear ideal, run-rate projection).
    """
    today = today or date.today()
    parent_map = parent_map or {}
    budgeted_ids = budgeted_ids or set()
    kind_map = kind_map or {}
    bill_occurrences = list(bill_occurrences or [])
    start, end, period_days = _month_bounds(month)

    if today < start:
        today_day = 0
    elif today > end:
        today_day = period_days
    else:
        today_day = (today - start).days + 1

    # A single-category scope only sees its own bills.
    if target_category_id is not None:
        bill_occurrences = [
            o for o in bill_occurrences if o.category_id == target_category_id
        ]
    bill_cats = {o.category_id for o in bill_occurrences}
    bills_total = sum((o.amount for o in bill_occurrences), _ZERO)

    # Outflow magnitude by day for the scope (total), split bill vs discretionary.
    by_day: dict[int, Decimal] = {}
    disc_by_day: dict[int, Decimal] = {}            # non-bill spend (run-rate base)
    bill_actual_by_cat: dict[int, Decimal] = {}     # bill-category actual through today
    for txn in perimeter_txns:
        if txn.amount >= 0:               # depletion = outflows only
            continue
        if txn.posted_date[:7] != month:
            continue
        bucket = nearest_budgeted_ancestor(
            txn.category_id, parent_map, budgeted_ids,
        )
        if target_category_id is None:
            if kind_map.get(txn.category_id, "expense") != "expense":
                continue
        elif not is_ancestor_or_self(target_category_id, bucket, parent_map):
            # ADR-170: scope covers the target's whole budgeted subtree, so a
            # group's burn-down matches the group's *available* — the roll-up
            # it is plotted against. An exact `bucket == target` test would
            # chart only the residual's spending against the whole group's
            # budget, and the line would never reach the floor.
            #
            # This is exactly equivalent to the old test for a leaf: a leaf has
            # no budgeted descendants by definition, so nothing can bucket
            # below it. Only groups — which could not be scoped before — see a
            # difference.
            continue
        day_idx = (date.fromisoformat(txn.posted_date) - start).days + 1
        if day_idx < 1 or day_idx > period_days:
            continue
        amt = -txn.amount
        by_day[day_idx] = by_day.get(day_idx, _ZERO) + amt
        if bucket in bill_cats:
            if day_idx <= today_day:
                bill_actual_by_cat[bucket] = (
                    bill_actual_by_cat.get(bucket, _ZERO) + amt
                )
        else:
            disc_by_day[day_idx] = disc_by_day.get(day_idx, _ZERO) + amt

    x_days = list(range(1, period_days + 1))

    # ── Ideal: staircase of planned bills + linear discretionary remainder. ──
    bill_step_by_day: dict[int, Decimal] = {}
    for o in bill_occurrences:
        bill_step_by_day[o.day] = bill_step_by_day.get(o.day, _ZERO) + o.amount
    disc_planned = total_planned - bills_total
    if disc_planned < _ZERO:
        disc_planned = _ZERO
    ideal: list[Decimal] = []
    bills_cum = _ZERO
    for d in x_days:
        bills_cum += bill_step_by_day.get(d, _ZERO)
        ideal.append(_round2(
            bills_cum + disc_planned * Decimal(d) / Decimal(period_days)
        ))

    # ── Actual: cumulative total outflow through today (no future actuals). ──
    actual_x: list[int] = []
    actual: list[Decimal] = []
    running = _ZERO
    for d in range(1, today_day + 1):
        running += by_day.get(d, _ZERO)
        actual_x.append(d)
        actual.append(_round2(running))
    actual_to_date = actual[-1] if actual else _ZERO

    # ── Unpaid bills: amount-match each occurrence against its category's actual.
    unpaid: list[tuple[int, Decimal]] = []   # (effective_day, unpaid_amount)
    for cat in bill_cats:
        budget_left = bill_actual_by_cat.get(cat, _ZERO)
        for o in sorted(
            (o for o in bill_occurrences if o.category_id == cat),
            key=lambda o: o.day,
        ):
            if budget_left >= o.amount:
                budget_left -= o.amount                      # fully paid
            else:
                unpaid_amt = o.amount - (budget_left if budget_left > _ZERO else _ZERO)
                budget_left = _ZERO
                eff_day = max(o.day, today_day)               # overdue → step at today
                unpaid.append((eff_day, _round2(unpaid_amt)))
    unpaid_cum_by_day: dict[int, Decimal] = {}
    for eff_day, amt in unpaid:
        unpaid_cum_by_day[eff_day] = unpaid_cum_by_day.get(eff_day, _ZERO) + amt

    # ── Discretionary run-rate (non-bill spend only). ──
    disc_to_date = sum(
        (v for d, v in disc_by_day.items() if d <= today_day), _ZERO,
    )
    disc_rate = (
        disc_to_date / Decimal(today_day) if today_day > 0 else _ZERO
    )

    # ── Projection: actual + unpaid bill steps + discretionary run-rate. ──
    proj_x: list[int] = []
    proj: list[Decimal] = []
    if 1 <= today_day < period_days:
        bills_added = _ZERO
        for d in range(today_day, period_days + 1):
            bills_added += unpaid_cum_by_day.get(d, _ZERO)
            proj_x.append(d)
            proj.append(_round2(
                actual_to_date + bills_added
                + disc_rate * Decimal(d - today_day)
            ))

    projected_end = (
        proj[-1] if proj else (actual[-1] if actual else _ZERO)
    )

    # ── The day the plan runs out (ADR-172). ──
    # Spending *exactly* the plan exhausts it, so the test is `>=`: remaining
    # hits zero and there is nothing left, which is what "runs out" means. A
    # non-positive plan has nothing to run out of — every day would qualify,
    # which is noise, not a warning.
    runs_out_day: Optional[int] = None
    if total_planned > _ZERO:
        for d, v in zip(actual_x, actual):        # already happened
            if v >= total_planned:
                runs_out_day = d
                break
        if runs_out_day is None:
            for d, v in zip(proj_x, proj):        # else, projected to
                if v >= total_planned:
                    runs_out_day = d
                    break

    return BurnDownData(
        month=month, scope_label=scope_label, period_days=period_days,
        today_day=today_day, total_planned=_round2(total_planned),
        x_days=x_days, actual_x=actual_x, actual=actual,
        ideal_x=x_days, ideal=ideal, proj_x=proj_x, proj=proj,
        projected_end=projected_end,
        projected_remaining=_round2(total_planned - projected_end),
        runs_out_day=runs_out_day,
    )
