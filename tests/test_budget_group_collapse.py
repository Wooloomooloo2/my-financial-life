"""The budget matrix's group rows: collapse, persistence, editability (ADR-170).

The calc-layer tree is covered Qt-free in ``test_budget_hierarchy.py``. This is
the *window* half:

- A group's roll-up row is **not editable** (there is no line to write to),
  while its children and 'Everything else' are.
- Collapsing a group hides exactly its subtree and leaves the roll-up visible —
  collapsing is lossless.
- The collapse survives reopening the window, is stored **per budget**, and a
  corrupt setting degrades to 'everything expanded' rather than breaking.
- Editing no longer throws the reader back to the top of the table — the
  reported scroll-position bug.

Needs PySide6; run offscreen:

    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_budget_group_collapse.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop import budget_calc as bc
from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.budget_window import BudgetWindow

_D = Decimal


def _setup():
    """The screenshot's shape: Bills budgeted *and* two of its children
    itemised, plus an unbudgeted child (Water) that must fall to the residual,
    and a standalone Food line that must stay a plain leaf."""
    db = Path(tempfile.mkdtemp(prefix="mfl_group_")) / "m.mfl"
    repo = Repository(db)
    acct = repo.create_account(
        name="Current", type_key="cash", currency="GBP",
        opening_balance=_D("5000.00"),
    )
    cats = {}
    cats["bills"] = repo.create_category("Bills", None, "expense")
    cats["cable"] = repo.create_category(
        "Cable and Internet", cats["bills"], "expense",
    )
    cats["council"] = repo.create_category(
        "Council Tax", cats["bills"], "expense",
    )
    cats["water"] = repo.create_category("Water", cats["bills"], "expense")
    cats["food"] = repo.create_category("Food", None, "expense")

    for key, amt, day in (
        ("cable", "-32.99", "05"), ("council", "-352.00", "10"),
        ("water", "-40.00", "12"), ("bills", "-100.00", "15"),
        ("food", "-529.28", "20"),
    ):
        repo.insert_transaction(
            account_id=acct.id, posted_date=f"2026-07-{day}",
            amount=_D(amt), payee_id=None, category_id=cats[key],
            status="cleared", memo="", import_hash=None, import_batch_id=None,
        )

    budget = repo.create_budget(
        name="B", start_month="2026-01", length_months=12,
    )
    repo.set_budget_accounts(budget.id, [(acct.id, "balance")])
    for key, amt in (
        ("bills", "482.00"), ("cable", "33.00"),
        ("council", "352.00"), ("food", "600.00"),
    ):
        lid = repo.add_budget_line(
            budget_id=budget.id, category_id=cats[key], role="bills",
            rollover="none",
        )
        repo.set_line_allocation(lid, "2026-07", _D(amt), scope="all")
    return repo, budget, cats


def _budget_rows(model):
    """label -> _Row, for the Budget metric row of each line."""
    return {
        model.data(model.index(i, 0), Qt.DisplayRole).strip(): r
        for i, r in enumerate(model._rows)
        if r.kind == "metric" and r.metric == "budget"
    }


def test_group_rollup_is_not_editable_but_its_parts_are() -> None:
    repo, _b, _c = _setup()
    win = BudgetWindow(repo)
    model = win._table.model()
    rows = _budget_rows(model)
    jul = 7   # label + Jan..Jul

    def editable(label):
        i = model._rows.index(rows[label])
        return bool(model.flags(model.index(i, jul)) & Qt.ItemIsEditable)

    assert "▾ Bills" in rows, sorted(rows)
    assert not editable("▾ Bills"), "a roll-up has no line to write to"
    assert editable("Cable and Internet")
    assert editable("Council Tax")
    assert editable("Everything else"), "the parent's own line lives here"
    assert editable("Food"), "a childless line stays a plain editable leaf"


def test_collapsing_hides_the_subtree_and_keeps_the_total() -> None:
    repo, _b, cats = _setup()
    win = BudgetWindow(repo)
    model = win._table.model()
    key = bc.group_key(cats["bills"])

    before = model.rowCount()
    win._toggle_collapse(key)
    rows = _budget_rows(model)
    assert "Cable and Internet" not in rows
    assert "Everything else" not in rows
    assert "Food" in rows, "a sibling outside the group is untouched"
    assert "▸ Bills" in rows, "the header stays, with a collapsed chevron"

    # The whole point: the roll-up is still the truth while collapsed.
    hdr = rows["▸ Bills"].matrix_row
    assert hdr.cells[6].allocation == _D("867.00")   # 482 + 33 + 352
    assert hdr.cells[6].actual == _D("524.99")       # 100 + 40 + 32.99 + 352

    win._toggle_collapse(key)
    assert model.rowCount() == before, "re-expanding restores every row"


def test_collapse_survives_a_reopen_and_is_per_budget() -> None:
    repo, budget, cats = _setup()
    win = BudgetWindow(repo)
    key = bc.group_key(cats["bills"])
    win._toggle_collapse(key)
    collapsed_count = win._table.model().rowCount()

    # A fresh window over the same file reads the remembered state.
    win2 = BudgetWindow(repo)
    assert win2._collapsed == {key}
    assert win2._table.model().rowCount() == collapsed_count

    # A *different* budget over the same categories collapses independently —
    # the two are different views, and the setting is keyed by budget id.
    other = repo.create_budget(
        name="Other", start_month="2026-01", length_months=12,
    )
    repo.set_budget_accounts(other.id, [])
    win3 = BudgetWindow(repo)
    win3._picker.setCurrentIndex(
        [win3._picker.itemData(i) for i in range(win3._picker.count())]
        .index(other.id)
    )
    assert win3._collapsed == set(), "another budget must not inherit it"
    # ...and switching back restores the first budget's state.
    win3._picker.setCurrentIndex(
        [win3._picker.itemData(i) for i in range(win3._picker.count())]
        .index(budget.id)
    )
    assert win3._collapsed == {key}


def test_a_corrupt_setting_degrades_to_expanded() -> None:
    """A hand-edited or half-written setting must never break the screen."""
    repo, _b, _c = _setup()
    repo.set_setting("budget/collapsed", "{not json at all")
    win = BudgetWindow(repo)
    assert win._collapsed == set()
    assert win._table.model().rowCount() > 0


def test_a_refresh_keeps_the_current_cell() -> None:
    """A model reset clears the current index, and ``_render`` runs after every
    edit — so committing an amount used to drop the cursor, killing the
    highlight and any arrow/tab onward from the cell just typed into."""
    repo, _b, _c = _setup()
    win = BudgetWindow(repo)
    win.resize(1180, 720)
    win.show()
    table = win._table
    table.setCurrentIndex(table.model().index(4, 3))
    _app.processEvents()

    win._render()                 # the refresh an edit triggers
    _app.processEvents()

    assert table.currentIndex().isValid(), "the cursor was dropped"
    assert (table.currentIndex().row(), table.currentIndex().column()) == (4, 3)


def test_a_shrinking_refresh_does_not_restore_off_the_end() -> None:
    """The restore must not point past a table that got shorter — removing a
    line, or zeroing a residual away, both shorten it."""
    repo, budget, _c = _setup()
    win = BudgetWindow(repo)
    win.show()
    table = win._table
    last = table.model().rowCount() - 1
    table.setCurrentIndex(table.model().index(last, 3))
    _app.processEvents()

    lines = repo.list_budget_lines(budget.id)
    repo.delete_budget_line(lines[-1].id)
    win._render()                 # fewer rows than the parked index
    _app.processEvents()

    cur = table.currentIndex()
    assert not cur.isValid() or cur.row() < table.model().rowCount()


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
