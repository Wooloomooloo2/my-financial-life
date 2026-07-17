"""The budget burn-down actually burns down, against the plan (ADR-172).

Two defects, one behind the other.

**It paced against the wrong number.** ``total_planned`` was the scope's
*available* — allocation **plus accumulated rollover**. On a real budget six
months of unspent surplus had inflated that to 5.4× the month's plan, so the
pacing line insisted you should have spent £10,909 by the 17th of a month you
had assigned £3,673 to, and the budget line (which sets the y-axis) squashed
the actual spending into the bottom 7% of the chart. A rollover surplus is a
buffer, not a target.

**And it was a burn-down that went up.** Every series climbed toward a
ceiling. It now descends from the plan to zero, and the day it reaches zero is
the day you run out — a reading a rising line cannot give.

What these lock down:

- ``_pacing_target`` is the **allocation**, never ``available`` — with the
  rollover-inflated shape that caused the bug as the regression case.
- ``runs_out_day`` — from the actuals when it has already happened, from the
  projection when it is coming, ``None`` when it isn't, and never a false
  alarm on a zero plan.
- ``projected_remaining`` reconciles with ``projected_end``.
- The verdict says the answer in words, and reddens only when over.

The calc half is Qt-free; the view half drives the real window offscreen.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_budget_burndown.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop import budget_calc as bc
from mfl_desktop.db.repository import PerimeterTxn, Repository
from mfl_desktop.ui import budget_monthly_view as mv
from mfl_desktop.ui.budget_window import BudgetWindow

_D = Decimal
_JULY = "2026-07"          # 31 days
from datetime import date

_TODAY = date(2026, 7, 17)


def _txn(tid: int, day: int, amount: str, cat: int = 1) -> PerimeterTxn:
    return PerimeterTxn(
        id=tid, account_id=1, posted_date=f"{_JULY}-{day:02d}",
        amount=_D(amount), category_id=cat,
    )


def _burn(spends, plan="1000.00"):
    return bc.compute_burndown(
        perimeter_txns=[_txn(i, d, a) for i, (d, a) in enumerate(spends)],
        month=_JULY, total_planned=_D(plan), today=_TODAY,
        kind_map={1: "expense"},
    )


# ── the day it runs out ──────────────────────────────────────────────────


def test_comfortably_under_never_runs_out() -> None:
    d = _burn([(3, "-100.00"), (10, "-120.00")])
    assert d.runs_out_day is None
    assert d.projected_remaining > 0


def test_a_projection_that_crosses_zero_names_the_day() -> None:
    """The whole payoff of inverting: spending £60/day against a £1,000 plan
    exhausts it before month end, and the chart can say *when*."""
    d = _burn([(day, "-60.00") for day in range(1, 17)])
    # £960 by day 16 → run-rate ~£56.5/day → crosses £1,000 within a day or two.
    assert d.runs_out_day is not None
    assert d.today_day <= d.runs_out_day <= 31
    assert d.projected_remaining < 0


def test_already_over_reports_the_day_it_happened() -> None:
    """When the actuals have already blown the plan, the run-out day is
    history, not a forecast — it must come from the actual series."""
    d = _burn([(2, "-400.00"), (4, "-700.00")])
    assert d.runs_out_day == 4, "the day the actuals crossed the plan"
    assert d.runs_out_day < d.today_day


def test_spending_exactly_the_plan_counts_as_running_out() -> None:
    """Remaining hits zero and there is nothing left — which is what the words
    'runs out' mean. The test is `>=`, not `>`."""
    d = _burn([(5, "-1000.00")])
    assert d.runs_out_day == 5


def test_a_zero_plan_is_not_a_false_alarm() -> None:
    """With nothing budgeted, every day would qualify as 'run out'. That is
    noise, not a warning — and the chart draws its empty state instead."""
    d = _burn([(3, "-50.00")], plan="0.00")
    assert d.runs_out_day is None


def test_projected_remaining_reconciles_with_projected_end() -> None:
    d = _burn([(3, "-100.00"), (10, "-120.00")])
    assert d.projected_remaining == _D("1000.00") - d.projected_end


# ── the pacing target ────────────────────────────────────────────────────


def _rollover_budget():
    """The shape that caused the bug: a rollover line underspent for months,
    so July's *available* balloons far past what July was assigned."""
    db = Path(tempfile.mkdtemp(prefix="mfl_burn_")) / "m.mfl"
    repo = Repository(db)
    acct = repo.create_account(
        name="Current", type_key="cash", currency="GBP",
        opening_balance=_D("20000.00"),
    )
    food = repo.create_category("Food", None, "expense")
    b = repo.create_budget(name="Main", start_month="2026-01", length_months=12)
    repo.set_budget_accounts(b.id, [(acct.id, "balance")])
    lid = repo.add_budget_line(
        budget_id=b.id, category_id=food, role="bills", rollover="accumulate",
    )
    repo.set_line_allocation(lid, "2026-01", _D("1000.00"), scope="all")
    # Jan–Jun: spend a fraction of the plan, so ~£5,400 of surplus piles up.
    for m in range(1, 7):
        repo.insert_transaction(
            account_id=acct.id, posted_date=f"2026-{m:02d}-10",
            amount=_D("-100.00"), payee_id=None, category_id=food,
            status="cleared", memo="", import_hash=None, import_batch_id=None,
        )
    repo.insert_transaction(
        account_id=acct.id, posted_date="2026-07-05", amount=_D("-200.00"),
        payee_id=None, category_id=food, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    win = BudgetWindow(repo)
    win._monthly._month = _JULY
    win._monthly._render_month()
    return win


def test_the_burndown_paces_against_the_plan_not_the_rollover_buffer() -> None:
    """The regression that made the chart useless. `available` here is many
    times the month's plan; pacing against it is what crushed the data into
    the bottom of the canvas and claimed the reader was behind on spending."""
    win = _rollover_budget()
    matrix = win._matrix
    mi = matrix.months.index(_JULY)
    exp = next(s for s in matrix.sections if s.kind == "expense")
    cell = exp.subtotal[mi]

    assert cell.available > cell.allocation * 4, (
        "test needs a rollover-inflated available to be meaningful"
    )
    assert mv._pacing_target(cell) == cell.allocation
    assert win._monthly._chart._data.total_planned == cell.allocation, (
        "the chart must pace against the month's plan, not the buffer"
    )


def test_the_pacing_target_ignores_a_carried_in_deficit_too() -> None:
    """Symmetry: if a surplus doesn't raise the target, a deficit doesn't lower
    it. The plan is the plan; the debt is reported on the row (ADR-171)."""
    class _Cell:
        allocation = _D("100.00")
        available = _D("-30.00")     # a carried-in overspend
    assert mv._pacing_target(_Cell()) == _D("100.00")


def test_no_cell_is_a_zero_target_not_a_crash() -> None:
    assert mv._pacing_target(None) == _D("0.00")


# ── the verdict ──────────────────────────────────────────────────────────


def _verdict(win) -> str:
    return re.sub("<[^>]+>", "", win._monthly._verdict.text()).replace(
        "&nbsp;", " ",
    )


def _spending_budget(spends, plan="1000.00"):
    db = Path(tempfile.mkdtemp(prefix="mfl_bv_")) / "m.mfl"
    repo = Repository(db)
    acct = repo.create_account(
        name="Current", type_key="cash", currency="GBP",
        opening_balance=_D("20000.00"),
    )
    food = repo.create_category("Food", None, "expense")
    b = repo.create_budget(name="Main", start_month="2026-01", length_months=12)
    repo.set_budget_accounts(b.id, [(acct.id, "balance")])
    lid = repo.add_budget_line(
        budget_id=b.id, category_id=food, role="bills", rollover="none",
    )
    repo.set_line_allocation(lid, "2026-01", _D(plan), scope="all")
    for day, amt in spends:
        repo.insert_transaction(
            account_id=acct.id, posted_date=f"{_JULY}-{day:02d}",
            amount=_D(amt), payee_id=None, category_id=food, status="cleared",
            memo="", import_hash=None, import_batch_id=None,
        )
    win = BudgetWindow(repo)
    win._monthly._month = _JULY
    win._monthly._render_month()
    return win


def test_the_verdict_states_the_answer_rather_than_implying_it() -> None:
    """`projected_end` was computed and read by nothing — the chart worked out
    the answer and left the reader to eyeball where a dashed line stopped."""
    win = _spending_budget([(3, "-100.00"), (10, "-50.00")])
    text = _verdict(win)
    assert "On track" in text
    assert "left on 31" in text
    assert "£" in text, "the verdict is money, and money carries its glyph"


def test_the_verdict_reddens_only_when_over() -> None:
    good = _spending_budget([(3, "-100.00")])
    assert mv._good_ink() in good._monthly._verdict.text()
    assert mv._bad_ink() not in good._monthly._verdict.text()

    bad = _spending_budget([(2, "-800.00"), (4, "-700.00")])
    assert "Over budget" in _verdict(bad)
    assert mv._bad_ink() in bad._monthly._verdict.text()


def test_the_verdict_names_the_buffer_without_pacing_against_it() -> None:
    """The rollover is still real money — it just isn't the target. The row
    states it (ADR-171) and the verdict mentions it; neither paces on it."""
    win = _rollover_budget()
    text = _verdict(win)
    assert "rolled over if needed" in text, text


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print("\n" + ("all passed" if not failures else f"{failures} failed"))
    sys.exit(1 if failures else 0)
