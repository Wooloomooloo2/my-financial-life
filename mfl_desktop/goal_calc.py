"""Goal computation — pure Python, no Qt, no SQL (ADR-058 R4b/R4c).

Turns a goal (a target balance + date) and its **already-aggregated** start /
current balances into the figures the UI shows: the work still to do, the
**required monthly contribution** to hit the target on time, and **balance-based
progress** (measured from the baseline captured at creation, so later activity
moves it).

A goal can span many accounts in possibly-different currencies (ADR-058 R4c).
The roll-up + FX conversion into the goal's currency happen in the Repository
(``compute_goal_aggregates``) / window, mirroring how ``compute_perimeter_pool``
feeds ``budget_calc`` — so this module stays pure and just does the math on the
two pre-converted signed Decimals.

The math runs on **signed balances**, so it works unchanged for a pay-down (a
liability climbing toward 0) and a savings goal (an asset climbing toward a
larger figure) — the ``kind`` flag carries no math, only labelling.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from mfl_desktop.db.repository import BudgetGoal

_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class GoalProgress:
    """Live view of one goal, in the goal's reporting currency (signed like a
    balance)."""
    goal_id: int
    name: str
    kind: str                # 'paydown' | 'savings'
    currency: str
    start_amount: Decimal    # signed baseline aggregate (at creation)
    current_amount: Decimal  # signed live aggregate
    target_amount: Decimal   # signed target balance
    target_date: str
    remaining: Decimal       # work still to do toward target (>= 0)
    months_left: int         # whole calendar months from today to target
    required_monthly: Decimal
    progress_pct: float      # 0..100, clamped
    is_met: bool
    is_overdue: bool
    account_count: int       # how many accounts contribute
    rate_missing: bool       # a contributing account couldn't be converted


def _parse(d: str) -> date:
    return date.fromisoformat(d)


def _months_between(a: date, b: date) -> int:
    """Whole calendar months from ``a`` to ``b`` (``b`` later → positive)."""
    return (b.year - a.year) * 12 + (b.month - a.month)


def compute_goal_progress(
    goal: BudgetGoal,
    *,
    start_amount: Decimal,
    current_amount: Decimal,
    today: date,
    rate_missing: bool = False,
) -> GoalProgress:
    """One goal's live figures from its (pre-aggregated, goal-currency) start +
    current balances. Pure — fixture-friendly via ``today``."""
    start = start_amount
    target = goal.target_amount
    span = target - start            # signed total distance to cover
    moved = current_amount - start   # signed distance covered so far

    if span == _ZERO:
        ratio = 1.0 if current_amount == target else 0.0
    else:
        ratio = float(moved / span)
    is_met = ratio >= 1.0
    progress_pct = max(0.0, min(1.0, ratio)) * 100.0

    # Work still to do, in the goal's direction (0 once met / overshot).
    if span > _ZERO:
        remaining = max(_ZERO, target - current_amount)
    elif span < _ZERO:
        remaining = max(_ZERO, current_amount - target)
    else:
        remaining = _ZERO

    tgt_date = _parse(goal.target_date)
    months_left = max(0, _months_between(today, tgt_date))
    # max(1, …) so a goal due this month (or overdue) asks for the whole
    # remaining rather than dividing by zero.
    required_monthly = (
        remaining / Decimal(max(1, months_left))
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    is_overdue = (tgt_date < today) and not is_met

    return GoalProgress(
        goal_id=goal.id, name=goal.name, kind=goal.kind, currency=goal.currency,
        start_amount=start, current_amount=current_amount,
        target_amount=target, target_date=goal.target_date,
        remaining=remaining, months_left=months_left,
        required_monthly=required_monthly, progress_pct=progress_pct,
        is_met=is_met, is_overdue=is_overdue,
        account_count=len(goal.accounts), rate_missing=rate_missing,
    )
