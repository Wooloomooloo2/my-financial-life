"""Per-account summary computation — pure Python, no Qt, no SQL.

The Repository hands in:

- every ``TransactionRow`` for the focus account, chronologically
  (``list_transactions_for_account``),
- the account's ``opening_balance``,
- the list of ``ScheduledTxnRow`` due through some future horizon
  (``list_schedules_due_through``),
- the focus ``account_id``.

This module turns those into:

- a :class:`BalanceFlowSeries` for the combo chart — bars of income above
  zero, bars of spending below zero, and a balance polyline per bucket;
- a :class:`PeriodSummary` for the "Report" panel — opening / inflows /
  outflows / closing for the whole period;
- a :class:`StatusBreakdown` for the right-hand info panel — recorded
  balance, cleared balance, uncleared count and amount;
- top-N payee and category rows for the bottom-of-screen breakdowns
  (strict outflow per ADR-018 / ADR-030);
- the next-N upcoming scheduled transactions for the Upcoming block.

Mirror of :mod:`mfl_desktop.budget_calc` in shape — see ADR-033.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from mfl_desktop.db.repository import ScheduledTxnRow, SplitLine, TransactionRow


# ── period presets ─────────────────────────────────────────────────────────

# Identifier used by the window's period selector. The labels are the
# button text; the helper functions below derive (period_start, period_end)
# from today + the selected key. The "custom" preset doesn't resolve to
# a fixed window — the calling window prompts the user for explicit
# from / to dates and bypasses ``period_bounds`` for that case.
#
# This set was updated in the ADR-033 amendment (2026-06-06) — previous
# presets ("30d","90d","ytd","1y","5y","all") were swapped for finance-
# native ranges plus a Custom escape hatch.
PERIOD_KEYS: tuple[str, ...] = ("quarter", "6m", "ytd", "1y", "3y", "custom")

PERIOD_LABELS: dict[str, str] = {
    "quarter": "Last Quarter",
    "6m":      "Last 6 months",
    "ytd":     "Year to date",
    "1y":      "Last 12 months",
    "3y":      "Last 3 years",
    "custom":  "Custom",
}


def period_bounds(
    key: str, today: date, earliest_txn_date: Optional[date] = None,
) -> tuple[date, date]:
    """Return (period_start, period_end) for the given preset.

    "Last Quarter" is a rolling 90-day window (consistent with "Last 6
    months" and "Last 12 months" being rolling, not calendar-period —
    if the owner wants previous-calendar-quarter semantics later, that's
    a small follow-up). ``earliest_txn_date`` is no longer consulted —
    the previous "All time" preset was retired in favour of "Custom"
    (the owner picks the bounds explicitly when they need a long tail).
    ``key == "custom"`` raises so a leaky caller doesn't silently use
    today's-only bounds — the window must supply its own dates."""
    if key == "quarter":
        return today - timedelta(days=90), today
    if key == "6m":
        return today - timedelta(days=180), today
    if key == "ytd":
        return date(today.year, 1, 1), today
    if key == "1y":
        return today - timedelta(days=365), today
    if key == "3y":
        return today - timedelta(days=3 * 365), today
    if key == "custom":
        raise ValueError(
            "period_bounds: 'custom' has no fixed window — "
            "the calling window supplies its own dates."
        )
    raise ValueError(f"Unknown period key: {key!r}")


_MONTH_ABBR_FOR_RANGE = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def fmt_date_range(start: date, end: date) -> str:
    """Compact human range — ``1 Jun → 6 Jun`` when the years agree,
    ``1 Jun 2025 → 6 Jun 2026`` otherwise. Avoids ``%-d`` (Windows
    pitfall — see CLAUDE_CONTEXT). Used by the summary and drill-down
    windows for the Custom-period chip + REPORT header label."""
    smonth = _MONTH_ABBR_FOR_RANGE[start.month - 1]
    emonth = _MONTH_ABBR_FOR_RANGE[end.month - 1]
    if start.year == end.year:
        return f"{start.day} {smonth} → {end.day} {emonth}"
    return (
        f"{start.day} {smonth} {start.year} "
        f"→ {end.day} {emonth} {end.year}"
    )


def period_display_label(
    key: str,
    custom_start: Optional[date] = None,
    custom_end: Optional[date] = None,
) -> str:
    """Human label for a period selection. For ``"custom"`` callers must
    supply both bounds — falls back to the bare ``"Custom"`` string if
    they're missing (defensive)."""
    if key == "custom":
        if custom_start is not None and custom_end is not None:
            return f"Custom: {fmt_date_range(custom_start, custom_end)}"
        return PERIOD_LABELS["custom"]
    return PERIOD_LABELS.get(key, key)


# ── granularity / bucketing ────────────────────────────────────────────────

GRANULARITIES: tuple[str, ...] = ("daily", "weekly", "monthly", "quarterly", "yearly")


def pick_granularity(period_days: int) -> str:
    """Auto-pick a bucket size from the period span so the chart shows
    roughly 5–60 buckets at the window's default size. Thresholds are
    deliberately loose — they only need to keep bar widths sensible."""
    if period_days <= 45:
        return "daily"
    if period_days <= 120:
        return "weekly"
    if period_days <= 800:           # ~26 months
        return "monthly"
    if period_days <= 2200:          # ~6 years
        return "quarterly"
    return "yearly"


def _bucket_start(d: date, granularity: str) -> date:
    """First date of the bucket containing ``d`` for the given granularity.
    Weekly buckets are Monday-anchored (matches the spending report)."""
    if granularity == "daily":
        return d
    if granularity == "weekly":
        return d - timedelta(days=d.weekday())
    if granularity == "monthly":
        return date(d.year, d.month, 1)
    if granularity == "quarterly":
        q = (d.month - 1) // 3 + 1
        return date(d.year, (q - 1) * 3 + 1, 1)
    if granularity == "yearly":
        return date(d.year, 1, 1)
    raise ValueError(f"Unknown granularity: {granularity!r}")


def _next_bucket_start(bucket_start: date, granularity: str) -> date:
    """Day after the bucket-end — i.e. the start of the next bucket."""
    if granularity == "daily":
        return bucket_start + timedelta(days=1)
    if granularity == "weekly":
        return bucket_start + timedelta(days=7)
    if granularity == "monthly":
        if bucket_start.month == 12:
            return date(bucket_start.year + 1, 1, 1)
        return date(bucket_start.year, bucket_start.month + 1, 1)
    if granularity == "quarterly":
        m = bucket_start.month + 3
        if m > 12:
            return date(bucket_start.year + 1, m - 12, 1)
        return date(bucket_start.year, m, 1)
    if granularity == "yearly":
        return date(bucket_start.year + 1, 1, 1)
    raise ValueError(f"Unknown granularity: {granularity!r}")


def _bucket_starts(
    period_start: date, period_end: date, granularity: str,
) -> list[date]:
    """All bucket-start dates covering [period_start, period_end] inclusive.
    The first bucket may begin BEFORE period_start (e.g. a monthly bucket
    when period_start is the 15th); txn filtering uses the bucket key, so
    aggregations remain correct."""
    if period_end < period_start:
        return []
    starts: list[date] = []
    cur = _bucket_start(period_start, granularity)
    while cur <= period_end:
        starts.append(cur)
        cur = _next_bucket_start(cur, granularity)
    return starts


_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _format_bucket_label(bucket_start: date, granularity: str) -> str:
    """Compact human label for a bucket. Avoids ``%-d`` (Windows pitfall —
    see CLAUDE_CONTEXT)."""
    if granularity == "daily":
        return f"{bucket_start.day} {_MONTH_ABBR[bucket_start.month - 1]}"
    if granularity == "weekly":
        return f"{bucket_start.day} {_MONTH_ABBR[bucket_start.month - 1]}"
    if granularity == "monthly":
        return f"{_MONTH_ABBR[bucket_start.month - 1]} {bucket_start.year % 100:02d}"
    if granularity == "quarterly":
        q = (bucket_start.month - 1) // 3 + 1
        return f"{bucket_start.year}-Q{q}"
    if granularity == "yearly":
        return str(bucket_start.year)
    return ""


# ── dataclasses returned to the UI ─────────────────────────────────────────


@dataclass(frozen=True)
class BalanceFlowBucket:
    """One bar pair + balance-line point in the combo chart.

    ``income`` and ``spending`` are positive magnitudes; the chart draws
    spending below the zero baseline. ``closing_balance`` is signed (a
    credit-card account stays negative)."""
    label: str
    income: Decimal
    spending: Decimal
    closing_balance: Decimal


@dataclass(frozen=True)
class BalanceFlowSeries:
    """Full chart payload — granularity + opening point + ordered buckets.

    The chart prepends an extra leading point at ``opening_balance`` so
    the line begins at the right value (not at zero) and walks through
    every bucket-end."""
    granularity: str
    opening_balance: Decimal       # signed; balance at period_start
    buckets: list[BalanceFlowBucket]
    period_start: date
    period_end: date


@dataclass(frozen=True)
class PeriodSummary:
    """The four numbers shown in the "Report" panel for the active period.

    ``inflows`` and ``outflows`` are positive magnitudes. ``opening`` and
    ``closing`` are signed (balance can be negative for credit cards)."""
    period_label: str
    opening_balance: Decimal
    inflows: Decimal
    outflows: Decimal
    closing_balance: Decimal


@dataclass(frozen=True)
class StatusBreakdown:
    """Recorded vs. cleared, and what's still in flight.

    ``Pending`` and ``Uncleared`` are grouped as "uncleared" (matches the
    user-facing language on the screen); ``Cleared`` and ``Reconciled``
    count as cleared. ``uncleared_amount`` is signed — the net effect on
    the recorded balance of the in-flight rows."""
    recorded_balance: Decimal
    cleared_balance: Decimal
    uncleared_count: int
    uncleared_amount: Decimal


@dataclass(frozen=True)
class TopNRow:
    """One row in the Top Payees / Top Categories panels.

    ``amount`` is a positive magnitude (strict outflow); ``proportion``
    is amount / total across all outflows in the period (used to size
    the bar fill). ``entity_id`` carries the payee_id / category_id that
    the bucket represents — populated so the drill-down (ADR-034) can
    filter against it. ``None`` means a synthetic bucket like
    ``(No payee)`` or any group of mixed-id rows; those rows aren't
    clickable in the UI."""
    label: str
    amount: Decimal
    proportion: float
    entity_id: Optional[int]


@dataclass(frozen=True)
class UpcomingScheduled:
    """One row in the right-column Upcoming block.

    ``amount`` is signed (positive = inflow into the focus account, e.g.
    an incoming transfer or salary). ``days_until`` is positive for
    future occurrences and 0 if due today; the projection horizon is set
    by the caller, so overdue items are absent unless the caller widened
    the lookback."""
    label: str
    amount: Decimal
    next_due_date: str
    days_until: int
    cadence: str


# ── helpers ────────────────────────────────────────────────────────────────


def _parse_date(date_str: str) -> date:
    return date.fromisoformat(date_str)


def _split_txns_by_period(
    txns: list[TransactionRow], period_start: date, period_end: date,
) -> tuple[list[TransactionRow], list[TransactionRow]]:
    """Returns (txns_before_period_start, txns_in_period).

    Caller passes ``txns`` already chronological. Anything after
    ``period_end`` is dropped — the screen never displays future-dated
    txns on the chart (those live in the Upcoming block via schedules)."""
    before: list[TransactionRow] = []
    in_period: list[TransactionRow] = []
    for t in txns:
        d = _parse_date(t.posted_date)
        if d < period_start:
            before.append(t)
        elif d <= period_end:
            in_period.append(t)
    return before, in_period


def earliest_txn_date(txns: list[TransactionRow]) -> Optional[date]:
    """First posted_date across the account's txns. Used to size the
    "All time" period."""
    if not txns:
        return None
    return _parse_date(txns[0].posted_date)


# ── computations ───────────────────────────────────────────────────────────


def compute_balance_flow_series(
    txns: list[TransactionRow],
    account_opening_balance: Decimal,
    period_start: date,
    period_end: date,
    granularity: str,
) -> BalanceFlowSeries:
    """Bucket the in-period txns by ``granularity``; emit income, spending,
    and closing balance per bucket.

    Running balance is signed. Closing balance for bucket N is the
    cumulative sum of every txn whose posted_date falls in or before
    that bucket, plus the account's opening balance."""
    before, in_period = _split_txns_by_period(txns, period_start, period_end)
    opening_at_period_start = account_opening_balance + sum(
        (t.amount for t in before), start=Decimal("0.00"),
    )
    starts = _bucket_starts(period_start, period_end, granularity)
    if not starts:
        return BalanceFlowSeries(
            granularity=granularity,
            opening_balance=opening_at_period_start,
            buckets=[],
            period_start=period_start,
            period_end=period_end,
        )

    # Build a "this bucket starts on …" list and pair each with the
    # exclusive end day; the final bucket is open-ended through period_end.
    boundaries: list[tuple[date, date]] = []
    for i, s in enumerate(starts):
        end_exclusive = starts[i + 1] if i + 1 < len(starts) else _next_bucket_start(s, granularity)
        boundaries.append((s, end_exclusive))

    # Walk txns chronologically; each row gets dropped into the bucket
    # whose [start, end) contains its posted_date.
    income_by_idx: dict[int, Decimal] = {i: Decimal("0.00") for i in range(len(boundaries))}
    spending_by_idx: dict[int, Decimal] = {i: Decimal("0.00") for i in range(len(boundaries))}

    j = 0
    for t in in_period:
        d = _parse_date(t.posted_date)
        # Advance the bucket cursor to the bucket that contains d.
        while j < len(boundaries) - 1 and d >= boundaries[j][1]:
            j += 1
        # Defensive: if d falls before the first bucket (shouldn't, but
        # the boundary may start earlier than period_start for monthly etc.
        # — that's fine, it lands in bucket 0 still).
        if d < boundaries[0][0]:
            continue
        if t.amount >= 0:
            income_by_idx[j] += t.amount
        else:
            spending_by_idx[j] += -t.amount

    # Closing balance per bucket — walk cumulatively.
    buckets: list[BalanceFlowBucket] = []
    running = opening_at_period_start
    for i, (s, _end) in enumerate(boundaries):
        running = running + income_by_idx[i] - spending_by_idx[i]
        buckets.append(BalanceFlowBucket(
            label=_format_bucket_label(s, granularity),
            income=income_by_idx[i],
            spending=spending_by_idx[i],
            closing_balance=running,
        ))

    return BalanceFlowSeries(
        granularity=granularity,
        opening_balance=opening_at_period_start,
        buckets=buckets,
        period_start=period_start,
        period_end=period_end,
    )


def compute_period_summary(
    txns: list[TransactionRow],
    account_opening_balance: Decimal,
    period_start: date,
    period_end: date,
    period_label: str,
) -> PeriodSummary:
    """Opening / inflows / outflows / closing for the whole period.

    Mirrors what :func:`compute_balance_flow_series` would produce
    summed across all its buckets, but computed directly to keep the
    rounding behaviour clearly that of summed signed Decimals."""
    before, in_period = _split_txns_by_period(txns, period_start, period_end)
    opening = account_opening_balance + sum(
        (t.amount for t in before), start=Decimal("0.00"),
    )
    inflows = sum(
        (t.amount for t in in_period if t.amount > 0),
        start=Decimal("0.00"),
    )
    outflows = sum(
        (-t.amount for t in in_period if t.amount < 0),
        start=Decimal("0.00"),
    )
    closing = opening + inflows - outflows
    return PeriodSummary(
        period_label=period_label,
        opening_balance=opening,
        inflows=inflows,
        outflows=outflows,
        closing_balance=closing,
    )


def compute_status_breakdown(
    txns: list[TransactionRow],
    account_opening_balance: Decimal,
) -> StatusBreakdown:
    """Recorded vs. cleared balances + the still-in-flight numbers.

    Takes the FULL chronological list (not period-filtered) because
    in-flight txns from outside the active period still affect "what
    cleared and what hasn't" today."""
    cleared = account_opening_balance
    uncleared = Decimal("0.00")
    uncleared_count = 0
    for t in txns:
        if t.status in ("Cleared", "Reconciled"):
            cleared += t.amount
        else:
            # Pending or Uncleared — counts toward the in-flight totals.
            uncleared += t.amount
            uncleared_count += 1
    return StatusBreakdown(
        recorded_balance=cleared + uncleared,
        cleared_balance=cleared,
        uncleared_count=uncleared_count,
        uncleared_amount=uncleared,
    )


def _top_n_by_id(
    period_txns: list[TransactionRow],
    id_of: callable,           # type: ignore[valid-type]
    label_of: callable,        # type: ignore[valid-type]
    none_label: str,
    n: int,
) -> list[TopNRow]:
    """Strict outflow aggregation (ADR-018) keyed by entity id.

    Rows whose ``id_of`` returns ``None`` fall into a single synthetic
    bucket labelled ``none_label`` (e.g. ``(No payee)``); that bucket's
    ``entity_id`` stays ``None`` and the drill-down treats it as
    non-clickable until a "rows with no payee" filter is wired (deferred
    in ADR-034)."""
    totals: dict[Optional[int], Decimal] = {}
    labels: dict[Optional[int], str] = {}
    for t in period_txns:
        if t.amount >= 0:
            continue
        eid = id_of(t)
        if eid not in labels:
            labels[eid] = none_label if eid is None else label_of(t)
        totals[eid] = totals.get(eid, Decimal("0.00")) + (-t.amount)
    grand_total = sum(totals.values(), start=Decimal("0.00"))
    rows = [
        TopNRow(
            label=labels[eid],
            amount=amount,
            proportion=float(amount / grand_total) if grand_total > 0 else 0.0,
            entity_id=eid,
        )
        for eid, amount in totals.items()
    ]
    rows.sort(key=lambda r: r.amount, reverse=True)
    return rows[:n]


def top_payees(period_txns: list[TransactionRow], n: int = 10) -> list[TopNRow]:
    """Top spend destinations in the period. Rows with no payee_id are
    grouped under '(No payee)' so they don't silently merge into the
    first named payee. The label uses ``payee_name`` as it appears on
    the txn row — round 1 of ADR-029 deliberately doesn't roll up the
    register display to canonicals; round 2 will canonicalise at
    import time and downstream rollups inherit the fix."""
    return _top_n_by_id(
        period_txns,
        id_of=lambda t: t.payee_id,
        label_of=lambda t: t.payee_name or "(No payee)",
        none_label="(No payee)",
        n=n,
    )


def top_categories(
    period_txns: list[TransactionRow],
    n: int = 10,
    split_lines_by_txn: Optional[dict[int, list[SplitLine]]] = None,
) -> list[TopNRow]:
    """Top spend categories in the period, strict-outflow (ADR-018).

    Split transactions (ADR-051) are unrolled: when ``split_lines_by_txn``
    carries lines for a parent txn, each line contributes to its own category
    (negative lines only) instead of the parent's Uncategorised bucket. A
    parent with no lines supplied falls back to its own category — so callers
    that don't fetch splits get the old, total-on-parent behaviour.

    Transfer-kind split lines (ADR-051 amendment) are skipped: a −£30
    "Transfer to Savings" line moves money, it isn't spend — consistent with the
    Spending report, which only aggregates ``category.kind = 'expense'``."""
    split_lines_by_txn = split_lines_by_txn or {}
    totals: dict[int, Decimal] = {}
    labels: dict[int, str] = {}

    def add(cid: int, label: str, outflow: Decimal) -> None:
        if cid not in labels:
            labels[cid] = label
        totals[cid] = totals.get(cid, Decimal("0.00")) + outflow

    for t in period_txns:
        lines = split_lines_by_txn.get(t.id)
        if lines:
            for ln in lines:
                if ln.amount >= 0 or ln.category_kind == "transfer":
                    continue
                add(ln.category_id, ln.category_name or "(Uncategorised)",
                    -ln.amount)
        else:
            if t.amount >= 0:
                continue
            add(t.category_id, t.category_name or "(Uncategorised)", -t.amount)

    grand_total = sum(totals.values(), start=Decimal("0.00"))
    rows = [
        TopNRow(
            label=labels[cid],
            amount=amount,
            proportion=float(amount / grand_total) if grand_total > 0 else 0.0,
            entity_id=cid,
        )
        for cid, amount in totals.items()
    ]
    rows.sort(key=lambda r: r.amount, reverse=True)
    return rows[:n]


def upcoming_scheduled(
    schedules: Iterable[ScheduledTxnRow],
    account_id: int,
    today: date,
    horizon_days: int = 30,
    n: int = 5,
) -> list[UpcomingScheduled]:
    """The next ``n`` schedules affecting the focus account that fall
    within ``horizon_days``.

    A schedule "affects" the focus account if either:
      - ``schedule.account_id == account_id`` (the schedule posts a txn
        on this account directly), or
      - ``schedule.transfer_to_account_id == account_id`` (the schedule
        is a transfer whose destination is the focus account).

    For transfer-IN schedules the displayed amount flips sign — from the
    focus account's POV, money lands in (positive)."""
    horizon = today + timedelta(days=horizon_days)
    rows: list[UpcomingScheduled] = []
    for s in schedules:
        affects_us = (
            s.account_id == account_id
            or s.transfer_to_account_id == account_id
        )
        if not affects_us:
            continue
        due = _parse_date(s.next_due_date)
        if due > horizon:
            continue
        days_until = (due - today).days
        if s.transfer_to_account_id == account_id and s.account_id != account_id:
            # Transfer landing in this account: source row has negative
            # estimated_amount (outflow on the SOURCE); from THIS account's
            # POV it's an inflow.
            display_amount = -s.estimated_amount
        else:
            display_amount = s.estimated_amount
        label = s.payee_name if s.payee_name else (s.category_name or s.transfer_to_account_name or "Scheduled")
        rows.append(UpcomingScheduled(
            label=label,
            amount=display_amount,
            next_due_date=s.next_due_date,
            days_until=days_until,
            cadence=s.cadence,
        ))
    rows.sort(key=lambda r: r.next_due_date)
    return rows[:n]


def count_scheduled_for_account(
    schedules: Iterable[ScheduledTxnRow], account_id: int,
) -> int:
    """Total active schedules that touch the focus account (used by the
    Summary block's "N scheduled transactions" line)."""
    n = 0
    for s in schedules:
        if s.account_id == account_id or s.transfer_to_account_id == account_id:
            n += 1
    return n
