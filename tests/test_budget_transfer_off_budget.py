"""Off-budget transfers don't appear in the budget (ADR-186).

A transfer counts toward the budget only when it is explicitly categorised into
a category that is in the budget setup. A cross-perimeter transfer with no
budgeted ancestor is an internal money movement, not budget activity, so it no
longer forms a synthetic 'Unbudgeted' row in the Transfers section — reversing
the ADR-024 default that every cross-perimeter transfer had to appear.

What this locks down:
- an off-budget transfer produces **no** Unbudgeted Transfers row;
- a transfer explicitly categorised into a **budgeted** transfer category is
  still counted (under that line);
- unbudgeted **income / expense** are untouched — they still surface.

Qt-free — ``python3 tests/test_budget_transfer_off_budget.py`` or under pytest.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import budget_calc as bc
from mfl_desktop.db.repository import BudgetLine, PerimeterTxn

_D = Decimal

# Category tree:
#   Groceries(1)   expense
#   Salary(2)      income
#   To Savings(3)  transfer  — budgeted
#   To ISA(4)      transfer  — NOT budgeted (off-budget)
_PARENT_MAP = {1: None, 2: None, 3: None, 4: None}
_KIND_MAP = {1: "expense", 2: "income", 3: "transfer", 4: "transfer"}
_NAMES = {1: "Groceries", 2: "Salary", 3: "To Savings", 4: "To ISA"}


class _Budget:
    id = 1
    currency = "GBP"

    def months(self) -> list[str]:
        return ["2026-01"]


def _line(line_id: int, cat: int) -> BudgetLine:
    return BudgetLine(
        id=line_id, budget_id=1, category_id=cat, category_name=_NAMES[cat],
        category_parent_name="", category_kind=_KIND_MAP[cat], role="",
        rollover="none", sort_order=0,
    )


def _txn(tid: int, cat: int, amount: str) -> PerimeterTxn:
    return PerimeterTxn(
        id=tid, account_id=1, posted_date="2026-01-15", amount=_D(amount),
        category_id=cat,
    )


def _matrix(lines, txns):
    return bc.compute_matrix(
        budget=_Budget(), lines=lines, allocations={},
        perimeter_txns=txns, parent_map=_PARENT_MAP, kind_map=_KIND_MAP,
        display_ccy="GBP",
    )


def _section(m, kind):
    return next((s for s in m.sections if s.kind == kind), None)


def test_off_budget_transfer_makes_no_unbudgeted_row() -> None:
    """A transfer into an unbudgeted transfer category (To ISA) must not create
    an Unbudgeted Transfers row — and with no budgeted transfer line, the
    Transfers section shouldn't appear at all."""
    m = _matrix(
        lines=[_line(10, 1)],                 # only Groceries budgeted
        txns=[_txn(1, 4, "-500.00")],         # a £500 off-budget transfer
    )
    assert _section(m, "transfer") is None, "off-budget transfer leaked a section"


def test_budgeted_transfer_is_still_counted() -> None:
    """A transfer explicitly categorised into a budgeted transfer category
    still appears under that line."""
    m = _matrix(
        lines=[_line(10, 1), _line(11, 3)],   # Groceries + To Savings budgeted
        txns=[
            _txn(1, 3, "-500.00"),            # budgeted transfer → counted
            _txn(2, 4, "-250.00"),            # off-budget transfer → dropped
        ],
    )
    tsec = _section(m, "transfer")
    assert tsec is not None, "the budgeted transfer line should render a section"
    labels = {r.label for r in tsec.rows}
    assert "To Savings" in labels
    assert "Unbudgeted" not in labels, "off-budget transfer still leaked in"
    savings = next(r for r in tsec.rows if r.label == "To Savings")
    assert savings.actual_total == _D("500.00")


def test_unbudgeted_income_and_expense_unaffected() -> None:
    """The change is transfer-only: unbudgeted income/expense still surface in
    their sections' Unbudgeted rows."""
    m = _matrix(
        lines=[_line(10, 1)],                 # only Groceries budgeted
        txns=[
            _txn(1, 2, "3000.00"),            # unbudgeted income (Salary)
            _txn(2, 1, "-40.00"),             # budgeted expense (Groceries)
            _txn(3, 4, "-500.00"),            # off-budget transfer → dropped
        ],
    )
    inc = _section(m, "income")
    assert inc is not None and any(r.is_unbudgeted for r in inc.rows), \
        "unbudgeted income should still show"
    assert _section(m, "transfer") is None, "off-budget transfer still leaked"


# ── bare-script runner ──────────────────────────────────────────────────────

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception as e:  # noqa: BLE001
            failures += 1
            print("FAIL", fn.__name__, "->", e)
    return failures


if __name__ == "__main__":
    sys.exit(_run_all())
