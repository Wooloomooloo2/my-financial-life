"""Budget hierarchy: groups, roll-ups and 'Everything else' (ADR-170).

A budgeted category with budgeted descendants renders as a *group*: a roll-up
header (own residual + every descendant), its children indented beneath, and a
'Everything else' residual row carrying the parent's own line — emitted only
when it holds money or spending.

What these lock down:

- A parent with **no** budgeted children stays a plain editable leaf (the
  pre-ADR-170 shape) — the group machinery costs nothing until you itemise.
- A group header's cells are the true roll-up, and it is **not** editable.
- 'Everything else' carries the parent's own `line_id` (so editing it writes
  the parent's allocation) and appears only when non-zero.
- **Section subtotals do not double-count** the group headers — the regression
  that would make every total wrong.
- Indentation replaces the '(Parent)' suffix, except at depth 0 where nothing
  else disambiguates.

Qt-free — ``python3 tests/test_budget_hierarchy.py`` or under pytest.
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


class _Budget:
    """The two attributes compute_matrix actually touches."""
    id = 1
    currency = "GBP"

    def months(self) -> list[str]:
        return ["2026-01", "2026-02"]


# Category tree: Bills(1) ▸ Cable(2), Council(3);  Food(4) standalone.
_PARENT_MAP = {1: None, 2: 1, 3: 1, 4: None, 5: 1}
_KIND_MAP = {1: "expense", 2: "expense", 3: "expense", 4: "expense",
             5: "expense"}
_NAMES = {1: "Bills", 2: "Cable and Internet", 3: "Council Tax", 4: "Food",
          5: "Water"}


def _line(line_id: int, cat: int, rollover: str = "none") -> BudgetLine:
    parent = _PARENT_MAP[cat]
    return BudgetLine(
        id=line_id, budget_id=1, category_id=cat, category_name=_NAMES[cat],
        category_parent_name=_NAMES[parent] if parent else "",
        category_kind="expense", role="bills", rollover=rollover, sort_order=0,
    )


def _txn(tid: int, cat: int, month: str, amount: str) -> PerimeterTxn:
    return PerimeterTxn(
        id=tid, account_id=1, posted_date=f"{month}-15", amount=_D(amount),
        category_id=cat,
    )


def _matrix(lines, allocations, txns):
    return bc.compute_matrix(
        budget=_Budget(), lines=lines, allocations=allocations,
        perimeter_txns=txns, parent_map=_PARENT_MAP, kind_map=_KIND_MAP,
        display_ccy="GBP",
    )


def _expense(m):
    return next(s for s in m.sections if s.kind == "expense")


def _row(m, label):
    return next(r for r in _expense(m).rows if r.label == label)


def test_lone_parent_stays_a_leaf() -> None:
    """Bills budgeted, nothing beneath it → a plain editable leaf. No group
    header, no 'Everything else' — you only pay for the tree once you ask."""
    m = _matrix(
        [_line(10, 1)],
        {(10, "2026-01"): _D("982.00"), (10, "2026-02"): _D("982.00")},
        [_txn(1, 2, "2026-01", "-33.00")],   # Cable rolls up into Bills
    )
    rows = [r for r in _expense(m).rows if not r.is_unbudgeted]
    assert len(rows) == 1
    bills = rows[0]
    assert bills.row_kind == "leaf"
    assert bills.depth == 0
    assert bills.is_editable
    assert not bills.is_group
    # Cable has no line of its own, so its spend buckets to Bills.
    assert bills.cells[0].actual == _D("33.00")


def test_itemising_a_child_makes_the_parent_a_group() -> None:
    """The ADR-170 shape: header rolls up, children indent, remainder trails."""
    m = _matrix(
        [_line(10, 1), _line(11, 2), _line(12, 3)],
        {
            (10, "2026-01"): _D("482.00"),   # Bills' own remainder
            (11, "2026-01"): _D("33.00"),    # Cable
            (12, "2026-01"): _D("352.00"),   # Council Tax
        },
        [
            _txn(1, 2, "2026-01", "-30.00"),   # Cable
            _txn(2, 3, "2026-01", "-352.00"),  # Council Tax
            _txn(3, 1, "2026-01", "-100.00"),  # straight to Bills = residual
            _txn(4, 5, "2026-01", "-40.00"),   # Water: unbudgeted child of Bills
        ],
    )
    labels = [r.label for r in _expense(m).rows]
    assert labels == [
        "Bills", "Cable and Internet", "Council Tax", "Everything else",
    ]

    header = _row(m, "Bills")
    assert header.row_kind == "group"
    assert header.is_group
    assert not header.is_editable, "a roll-up is a sum, not a stored amount"
    assert header.depth == 0

    # Roll-up = own residual + every budgeted descendant.
    assert header.cells[0].allocation == _D("867.00")   # 482 + 33 + 352
    assert header.cells[0].actual == _D("522.00")       # 100 + 40 + 30 + 352

    kids = [_row(m, "Cable and Internet"), _row(m, "Council Tax")]
    assert all(k.depth == 1 and k.row_kind == "leaf" for k in kids)
    assert all(k.is_editable for k in kids)

    # 'Everything else' is Bills' own line — same line_id, still editable.
    residual = _row(m, "Everything else")
    assert residual.depth == 1
    assert residual.row_kind == "residual"
    assert residual.line_id == 10, "editing it must write Bills' allocation"
    assert residual.is_editable
    assert residual.cells[0].allocation == _D("482.00")
    # Bills' own bucket: the direct txn + the unbudgeted child Water.
    assert residual.cells[0].actual == _D("140.00")


def test_residual_hidden_when_fully_itemised() -> None:
    """Itemise a group to the penny and 'Everything else' disappears — the
    anti-clutter rule. The header still rolls the children up."""
    m = _matrix(
        [_line(10, 1), _line(11, 2)],
        {(11, "2026-01"): _D("33.00")},   # Bills' own line: nothing at all
        [_txn(1, 2, "2026-01", "-30.00")],
    )
    labels = [r.label for r in _expense(m).rows]
    assert "Everything else" not in labels
    assert labels == ["Bills", "Cable and Internet"]
    assert _row(m, "Bills").cells[0].allocation == _D("33.00")


def test_subtotal_does_not_double_count_the_group_header() -> None:
    """The regression that would poison every total: the section subtotal must
    sum the real budget lines, not the arranged rows (where a header restates
    its whole subtree)."""
    m = _matrix(
        [_line(10, 1), _line(11, 2), _line(12, 3)],
        {
            (10, "2026-01"): _D("482.00"),
            (11, "2026-01"): _D("33.00"),
            (12, "2026-01"): _D("352.00"),
        },
        [_txn(1, 2, "2026-01", "-30.00"), _txn(2, 1, "2026-01", "-100.00")],
    )
    sub = _expense(m).subtotal[0]
    assert sub.allocation == _D("867.00"), "482 + 33 + 352, each counted once"
    assert sub.actual == _D("130.00")
    # The header alone already equals the section — proof the subtotal isn't
    # the sum of the *rows* as rendered.
    assert _row(m, "Bills").cells[0].allocation == sub.allocation


def test_indent_replaces_the_parenthetical_but_only_where_it_can() -> None:
    """Nested, the position says '(Bills)'. Orphaned at depth 0 — parent not
    budgeted — the suffix is the only disambiguator, so it stays."""
    # Nested: Bills is budgeted, so Cable sits under it and reads bare.
    m = _matrix(
        [_line(10, 1), _line(11, 2)],
        {(10, "2026-01"): _D("482.00"), (11, "2026-01"): _D("33.00")},
        [],
    )
    assert _row(m, "Cable and Internet").depth == 1

    # Orphaned: only Cable is budgeted — it has nothing to nest under.
    m2 = _matrix([_line(11, 2)], {(11, "2026-01"): _D("33.00")}, [])
    orphan = _expense(m2).rows[0]
    assert orphan.depth == 0
    assert orphan.label == "Cable and Internet (Bills)"


def test_grandchildren_nest_and_roll_up_transitively() -> None:
    """Depth is not capped at one. A three-level chain rolls up through every
    level, and each level's header sees its whole subtree."""
    parent_map = {1: None, 2: 1, 6: 2}
    kind_map = {1: "expense", 2: "expense", 6: "expense"}
    names = {**_NAMES, 6: "Broadband"}

    def line(lid, cat):
        p = parent_map[cat]
        return BudgetLine(
            id=lid, budget_id=1, category_id=cat, category_name=names[cat],
            category_parent_name=names[p] if p else "",
            category_kind="expense", role="bills", rollover="none",
            sort_order=0,
        )

    m = bc.compute_matrix(
        budget=_Budget(),
        lines=[line(10, 1), line(11, 2), line(12, 6)],
        allocations={
            (10, "2026-01"): _D("100.00"),
            (11, "2026-01"): _D("20.00"),
            (12, "2026-01"): _D("5.00"),
        },
        perimeter_txns=[], parent_map=parent_map, kind_map=kind_map,
        display_ccy="GBP",
    )
    rows = {r.label: r for r in _expense(m).rows}
    assert rows["Bills"].depth == 0 and rows["Bills"].is_group
    assert rows["Cable and Internet"].depth == 1
    assert rows["Cable and Internet"].is_group, "a middle node is both"
    assert rows["Broadband"].depth == 2
    # Bills rolls up the whole chain; Cable rolls up only its own subtree.
    assert rows["Bills"].cells[0].allocation == _D("125.00")
    assert rows["Cable and Internet"].cells[0].allocation == _D("25.00")
    assert _expense(m).subtotal[0].allocation == _D("125.00")


def test_is_ancestor_or_self() -> None:
    """The drill-down's containment test for a group's roll-up."""
    assert bc.is_ancestor_or_self(1, 1, _PARENT_MAP)
    assert bc.is_ancestor_or_self(1, 2, _PARENT_MAP)
    assert not bc.is_ancestor_or_self(2, 1, _PARENT_MAP)
    assert not bc.is_ancestor_or_self(1, 4, _PARENT_MAP)
    assert not bc.is_ancestor_or_self(1, None, _PARENT_MAP)


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
