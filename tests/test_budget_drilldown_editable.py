"""The budget Actual drill-down is an editable register.

Regression guard for the bug where double-clicking a budget Actual cell opened
the drill-down, but inside it you could neither edit the transactions nor see a
split's lines. The window (`BudgetDrillDownWindow`) attached the register's
inline delegates but — unlike the register and the shared drill-down (ADR-147)
— never set edit triggers and had no `doubleClicked` handler. Split (and
investment) rows are non-editable inline by design (`register_model.flags`), so
with no double-click → dialog they could only be viewed, and their splits were
invisible.

Offscreen (PySide6):
    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_budget_drilldown_editable.py
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

from PySide6.QtWidgets import QAbstractItemView, QApplication

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.budget_drilldown_window import BudgetDrillDownWindow


def _setup():
    """A cash account with one plain and one split transaction — the two row
    shapes the drill-down has to handle."""
    db = Path(tempfile.mkdtemp(prefix="mfl_budgetdrill_")) / "m.mfl"
    repo = Repository(db)
    chk = repo.create_account(name="Everyday Current", type_key="cash",
                              currency="GBP", opening_balance=Decimal("0"))
    groceries = repo.create_category("Groceries", None, "expense")
    household = repo.create_category("Household", None, "expense")
    plain_id = repo.insert_transaction(
        account_id=chk.id, posted_date="2026-04-10", amount=Decimal("-40.00"),
        payee_id=None, category_id=groceries, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    split_id = repo.insert_split_transaction(
        account_id=chk.id, posted_date="2026-04-15", payee_id=None,
        status="cleared", memo="", total_amount=Decimal("-120.00"),
        lines=[
            (groceries, None, Decimal("-100.00")),
            (household, None, Decimal("-20.00")),
        ],
        import_hash=None, import_batch_id=None,
    )
    repo.commit()
    return repo, {plain_id, split_id}, split_id, plain_id


def _win(repo, ids):
    return BudgetDrillDownWindow(
        repo, txn_ids=ids, title="Groceries — April 2026",
        net=Decimal("-160.00"), display_ccy="GBP",
    )


def _row_for(proxy, model, pred):
    return [
        r for r in range(proxy.rowCount())
        if pred(model.row_at(proxy.mapToSource(proxy.index(r, 0)).row()))
    ]


def test_edit_triggers_are_set():
    """The window must set the register's edit triggers so plain rows are
    inline-editable (the bug: it set none, so nothing could be edited)."""
    repo, ids, _split, _plain = _setup()
    win = _win(repo, ids)
    try:
        trig = win._table.editTriggers()
        assert trig & QAbstractItemView.DoubleClicked
        assert trig & QAbstractItemView.EditKeyPressed
    finally:
        win.close()


def test_split_row_double_click_opens_split_dialog():
    """A split row's double-click routes to the split dialog — so its lines
    are visible and editable, the reported bug."""
    repo, ids, split_id, _plain = _setup()
    win = _win(repo, ids)
    try:
        proxy, model = win._proxy, win._model
        split_rows = _row_for(proxy, model, lambda row: row.split_count)
        assert split_rows, "the split parent should be listed"
        seen = {}
        win._open_split_txn_dialog = lambda seed: seen.setdefault("id", seed.id)
        win._on_table_double_clicked(proxy.index(split_rows[0], 0))
        assert seen.get("id") == split_id
    finally:
        win.close()


def test_plain_row_double_click_stays_inline():
    """A plain cash row must NOT be hijacked to a dialog — Qt's own inline
    edit trigger handles it (the handler is a no-op for it)."""
    repo, ids, _split, _plain = _setup()
    win = _win(repo, ids)
    try:
        proxy, model = win._proxy, win._model
        plain_rows = _row_for(
            proxy, model,
            lambda row: not row.split_count and row.action is None,
        )
        assert plain_rows, "the plain cash row should be listed"
        opened = {"split": False, "inv": False}
        win._open_split_txn_dialog = lambda seed: opened.__setitem__("split", True)
        win._open_investment_txn_dialog = lambda seed: opened.__setitem__("inv", True)
        win._on_table_double_clicked(proxy.index(plain_rows[0], 0))
        assert opened == {"split": False, "inv": False}
    finally:
        win.close()


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
