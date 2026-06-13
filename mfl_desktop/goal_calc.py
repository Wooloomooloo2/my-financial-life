"""Goal computation — pure Python, no Qt, no SQL (ADR-058 R4b).

Turns a stored goal (a target balance + date plus the baseline captured at
creation) and an account's live balance into the figures the UI shows: the work
still to do, the **required monthly contribution** to hit the target on time,
and **balance-based progress** (measured from the captured baseline, so later
charges that push a card's balance back up visibly reduce progress).

The math runs on **signed balances**, so it works unchanged for a pay-down (a
liability climbing toward 0) and a savings goal (an asset climbing toward a
larger figure) — the R4c direction is the very same formula, which is why the
``kind`` flag carries no math, only labelling.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from mfl_desktop.db.repository import BudgetGoal

_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class GoalProgress:
    """Live view of one goal. Amounts are in the account's own currency (a goal
    is single-account, so no FX) and signed like a balance."""
    goal_id: int
    account_id: int
    account_name: str
    kind: str                # 'paydown' | 'savings'
    currency: str
    start_amount: Decimal    # signed baseline balance (at creation)
    current_amount: Decimal  # signed live balance
    target_amount: Decimal   # signed target balance
    target_date: str
    remaining: Decimal       # work still to do toward target (>= 0)
    months_left: int         # whole calendar months from today to target
    required_monthly: Decimal
    progress_pct: float      # 0..100, clamped
    is_met: bool
    is_overdue: bool


def _parse(d: str) -> date:
    return date.fromisoformat(d)


def _months_between(a: date, b: date) -> int:
    """Whole calendar months from ``a`` to ``b`` (``b`` later → positive)."""
    return (b.year - a.year) * 12 + (b.month - a.month)


def compute_goal_progress(
    goal: BudgetGoal,
    *,
    current_amount: Decimal,
    account_name: str,
    currency: str,
    today: date,
) -> GoalProgress:
    """One goal's live figures. Pure — fixture-friendly via ``today``."""
    start = goal.start_amount
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
        goal_id=goal.id, account_id=goal.account_id,
        account_name=account_name, kind=goal.kind, currency=currency,
        start_amount=start, current_amount=current_amount,
        target_amount=target, target_date=goal.target_date,
        remaining=remaining, months_left=months_left,
        required_monthly=required_monthly, progress_pct=progress_pct,
        is_met=is_met, is_overdue=is_overdue,
    )


def compute_goals(
    goals: Iterable[BudgetGoal],
    *,
    balances: dict[int, Decimal],
    account_info: dict[int, tuple[str, str]],   # account_id -> (name, currency)
    today: date,
) -> list[GoalProgress]:
    """Compute progress for many goals against current balances. Thin
    orchestration so the window stays declarative; still pure (no Repository,
    no Qt)."""
    out: list[GoalProgress] = []
    for g in goals:
        name, ccy = account_info.get(g.account_id, ("?", ""))
        out.append(compute_goal_progress(
            g, current_amount=balances.get(g.account_id, _ZERO),
            account_name=name, currency=ccy, today=today,
        ))
    return out
