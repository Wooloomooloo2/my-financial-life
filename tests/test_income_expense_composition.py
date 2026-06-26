"""Top-level breakdown roll-up for the Income & Expense donut (ADR-113).

`compose_top_level` walks per-leaf-category pence up to each leaf's top-level
ancestor, returns the slices largest-first, folds the tail past `top_n` into a
single "Other" bucket, and drops zero/negative totals. Pure + Qt-free — runs on
the base interpreter or under pytest:

    python3 tests/test_income_expense_composition.py
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mfl_desktop.reports.income_expense import (  # noqa: E402
    CompositionSlice, compose_top_level,
)


# A small two-level tree: two top-level categories, each with children, plus a
# lone top-level leaf.
PARENT_OF = {1: None, 2: 1, 3: 1, 10: None, 11: 10, 12: 10, 99: None}
NAME_OF = {
    1: "Income", 2: "Salary", 3: "Interest",
    10: "Bills", 11: "Rent", 12: "Utilities", 99: "Gifts",
}


def test_rolls_leaves_up_to_top_level() -> None:
    leaf = {2: 500000, 3: 2500, 11: 120000, 12: 30000, 99: 1000}
    slices = compose_top_level(leaf, PARENT_OF, NAME_OF)
    by_label = {s.label: s for s in slices}
    # Income = Salary + Interest; Bills = Rent + Utilities; Gifts standalone.
    assert by_label["Income"].value == Decimal("5025.00")
    assert by_label["Income"].category_id == 1
    assert by_label["Bills"].value == Decimal("1500.00")
    assert by_label["Gifts"].value == Decimal("10.00")
    # Sorted largest-first.
    assert [s.label for s in slices] == ["Income", "Bills", "Gifts"]


def test_overflow_folds_into_other() -> None:
    leaf = {i: (1000 - i) for i in range(1, 20)}
    parent = {i: None for i in range(1, 20)}
    name = {i: f"c{i}" for i in range(1, 20)}
    slices = compose_top_level(leaf, parent, name, top_n=8)
    assert len(slices) == 9
    other = slices[-1]
    assert other.label == "Other" and other.category_id is None
    # Other = sum of the 11 trimmed leaves (c9..c19), in major units.
    assert other.value == Decimal(sum(1000 - i for i in range(9, 20))) / Decimal(100)


def test_zero_and_negative_dropped() -> None:
    assert compose_top_level(
        {1: 0, 2: -5}, {1: None, 2: None}, {1: "a", 2: "b"},
    ) == []


def test_broken_chain_rolls_to_furthest_ancestor() -> None:
    # 5's parent 4 has no entry in the parent map → rolls up to 4, not crash.
    slices = compose_top_level(
        {5: 1000}, {5: 4}, {4: "Stub", 5: "Leaf"},
    )
    assert len(slices) == 1
    assert slices[0].category_id == 4 and slices[0].label == "Stub"


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
