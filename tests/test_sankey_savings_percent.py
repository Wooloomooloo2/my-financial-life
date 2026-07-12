"""The Savings node is a percentage of income, not of expenditure (ADR-155).

Owner report: the Cash Flow Sankey showed "Savings 98%" while the summary rail
on the same screen said "Saving rate: 49.4% of income".

Savings is ``income - expense``. It is appended to the *expense* side so the two
sides fill the spine, but it is deliberately NOT part of ``total_expense`` — so
dividing it by expenditure measured it against a total that excluded it:

    24,216 / 24,763 = 97.8%  ->  "98%"      (wrong: the denominator is spending)
    24,216 / 48,978 = 49.4%  ->  "49%"      (right: the denominator is income)

The invariant that must hold: **the Savings node's percentage and the rail's
saving rate are the same number.** They describe the same quantity and sit on the
same screen.

Run headless:

    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_sankey_savings_percent.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from mfl_desktop.ui.sankey_chart import SankeyChart, SankeyNode

_C = "#3b82f6"          # colour is required but irrelevant to the percentages here

# The owner's reported figures.
INCOME = 48_978.0
EXPENSE = 24_763.0
SAVED = INCOME - EXPENSE          # 24,215


def _chart(income_nodes, expense_nodes, total_income, total_expense) -> SankeyChart:
    c = SankeyChart()
    c.render(
        income=income_nodes, expense=expense_nodes,
        total_income=total_income, total_expense=total_expense,
        value_mode="percent",
    )
    return c


def _savings_chart() -> tuple[SankeyChart, SankeyNode]:
    savings = SankeyNode(label="Savings", value=SAVED, color=_C, is_balance=True)
    household = SankeyNode(label="Household", value=6_933.0, color=_C)
    pay = SankeyNode(label="Mark Net Pay", value=INCOME, color=_C)
    return _chart([pay], [household, savings], INCOME, EXPENSE), savings


def test_savings_is_a_share_of_income_not_expenditure():
    c, savings = _savings_chart()
    denom = c._denominator_for(savings, 1)      # col > 0 == the expense side
    assert denom == INCOME
    pct = savings.value / denom * 100.0
    assert round(pct, 1) == 49.4


def test_savings_percent_is_not_the_old_98():
    """The specific regression: divided by expenditure it read ~98%."""
    c, savings = _savings_chart()
    wrong = savings.value / EXPENSE * 100.0
    assert round(wrong) == 98          # what the bug produced
    right = savings.value / c._denominator_for(savings, 1) * 100.0
    assert round(right) == 49          # what it must produce now


def test_savings_label_agrees_with_the_summary_rails_saving_rate():
    """The invariant. Both numbers describe income - expense over income."""
    c, savings = _savings_chart()
    label = c._amount_label(savings.value, c._denominator_for(savings, 1))
    rail_rate = SAVED / INCOME * 100.0                 # what _update_summary computes
    assert label == f"{rail_rate:.0f}%"


def test_expense_categories_still_share_expenditure():
    """Unchanged: a spending category still reads as a % of spending, so
    'Household is 28% of what I spend' still holds."""
    c, _savings = _savings_chart()
    household = c._expense[0]
    assert not household.is_balance
    assert c._denominator_for(household, 1) == EXPENSE


def test_income_categories_still_share_income():
    c, _savings = _savings_chart()
    pay = c._income[0]
    assert c._denominator_for(pay, -1) == INCOME


def test_deficit_is_also_a_share_of_income():
    """The mirror case, and the one that was already right — Deficit lands on the
    income side, so it already divided by income. It must stay that way."""
    over_expense = 60_000.0
    deficit_value = over_expense - INCOME
    deficit = SankeyNode(label="Deficit", value=deficit_value, color=_C, is_balance=True)
    c = _chart([deficit], [SankeyNode(label="Household", value=over_expense, color=_C)],
               INCOME, over_expense)
    assert c._denominator_for(deficit, -1) == INCOME


def test_spine_node_still_measures_against_the_larger_side():
    c, _savings = _savings_chart()
    spine = SankeyNode(label="spine", value=INCOME, color=_C)
    assert c._denominator_for(spine, 0) == max(INCOME, EXPENSE)


# ── bare-script runner ──────────────────────────────────────────────────────

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
