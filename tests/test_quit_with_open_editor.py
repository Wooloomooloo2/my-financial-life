"""Quitting with a cell editor open doesn't crash (ADR-191).

Owner-reported on quit:

    ProgrammingError: Error calling Python override of
    QStyledItemDelegate::setModelData(): Error calling Python override of
    QAbstractTableModel::setData(): Cannot operate on a closed database.

The shape: ``RegisterWindow._flush_and_close`` closes the Repository on quit,
then Qt tears the view down — and tearing down a view with an **open cell
editor** commits that editor on the way out. The commit runs
``delegate.setModelData`` → ``model.setData`` → the Repository, which is now
closed. Because it happens inside a Qt virtual, the ``sqlite3.ProgrammingError``
surfaces with that double "Error calling Python override" wrapper rather than a
normal traceback.

This is the *edit* variant of the bug the ADR-109 follow-up fixed for
activate-refresh (``tests/test_shutdown_refresh_guard.py``), and it takes the
same guard — plus an ordering fix, so a genuine in-flight edit is committed
while the connection is still open instead of being silently dropped.

Run: ``venv/bin/pytest tests/test_quit_with_open_editor.py``
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

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.register_window import RegisterWindow


def _build():
    db = Path(tempfile.mkdtemp(prefix="mfl_quit_")) / "money.mfl"
    repo = Repository(db)
    acct = repo.create_account(
        name="Current", type_key="cash", currency="GBP",
    )
    cat = repo.create_category("Food", None, "expense")
    for i in range(3):
        repo.insert_transaction(
            account_id=acct.id, posted_date=f"2026-08-1{i}",
            amount=Decimal("-10.00"), payee_id=None, category_id=cat,
            status="cleared", memo=f"row {i}",
            import_hash=None, import_batch_id=None,
        )
    repo.commit()
    return repo, acct


def _open(repo, account):
    win = RegisterWindow(repo, account.iri)
    win._window_key = "all"
    win._model.set_since(win._effective_since())
    return win


def _memo_index(win):
    model = win._model
    col = model._column_index("memo")
    assert col is not None, "the register has no memo column"
    return model.index(0, col)


# ── the crash ───────────────────────────────────────────────────────────────


def test_setdata_after_close_is_declined_not_fatal():
    """The exact call Qt makes during teardown. Before ADR-191 this raised
    sqlite3.ProgrammingError straight out of a Qt virtual."""
    repo, acct = _build()
    win = _open(repo, acct)
    idx = _memo_index(win)
    repo.close()

    assert win._model.setData(idx, "typed while quitting", Qt.EditRole) is False


def test_a_full_quit_with_an_open_editor_does_not_raise():
    """End to end: open an editor on a cell, then quit. The editor is torn down
    with the window, which is what fired the commit into a closed database."""
    repo, acct = _build()
    win = _open(repo, acct)
    win.show()
    for _ in range(5):
        _app.processEvents()

    idx = _memo_index(win)
    proxy_idx = win._proxy.mapFromSource(idx)
    win._table.setCurrentIndex(proxy_idx)
    win._table.edit(proxy_idx)          # a live editor, as if mid-typing
    for _ in range(5):
        _app.processEvents()

    win.on_about_to_quit()              # the Cmd-Q path (ADR-109)
    win.close()
    for _ in range(5):
        _app.processEvents()
    win.deleteLater()
    for _ in range(5):
        _app.processEvents()

    assert not repo.is_open()


# ── the in-flight edit is saved, not dropped ────────────────────────────────


def test_an_open_editors_value_is_committed_before_the_close():
    """The ordering half. Quitting mid-edit should keep what was typed —
    clearing focus makes the delegate commit while the connection is alive.
    Without it the guard alone would silently discard the edit."""
    repo, acct = _build()
    win = _open(repo, acct)
    win.show()
    for _ in range(5):
        _app.processEvents()

    idx = _memo_index(win)
    proxy_idx = win._proxy.mapFromSource(idx)
    txn_id = win._model.row_at(0).id
    win._table.setCurrentIndex(proxy_idx)
    win._table.edit(proxy_idx)
    for _ in range(5):
        _app.processEvents()

    editor = win._table.focusWidget()
    if editor is None or not hasattr(editor, "setText"):
        # No editor widget under this platform plugin — the ordering can't be
        # observed here, and the guard test above already covers the crash.
        return
    editor.setText("saved on quit")
    win.on_about_to_quit()

    reopened = Repository(repo.db_path)
    row = next(
        r for r in reopened.list_transactions_for_account(acct.id)
        if r.id == txn_id
    )
    assert row.memo == "saved on quit"


# ── the budget matrix has the same exposure ─────────────────────────────────


def test_budget_allocation_edit_after_close_is_declined(monkeypatch):
    """The budget matrix is editable too (its cells write through a callback,
    not through the register model), so it needs its own guard — and it must
    decline BEFORE the copy-forward prompt, since a modal question during
    shutdown would be worse than losing the figure.

    The prompt is stubbed rather than left live: without the guard the real one
    opens and blocks the run forever, and a test that hangs tells you nothing.
    Stubbing it to a *recorded* answer keeps the assertion honest — reaching
    the prompt at all is the failure."""
    from mfl_desktop.ui import budget_window as bw
    from mfl_desktop.ui.budget_window import BudgetWindow

    prompted = []
    monkeypatch.setattr(
        bw, "_ask_copy_forward_scope",
        lambda *a, **kw: prompted.append(a) or "all",
    )
    # ...and the error box the handler falls into when the write then fails.
    # Unstubbed, that modal is the *actual* hang without the guard: the
    # ProgrammingError is caught and turned into a dialog that waits forever.
    complained = []
    monkeypatch.setattr(
        bw.QMessageBox, "critical",
        staticmethod(lambda *a, **kw: complained.append(a)),
    )

    repo, acct = _build()
    budget = repo.create_budget(
        name="B", start_month="2026-01", length_months=12, currency="GBP",
    )
    cat = repo.create_category("Bills", None, "expense")
    line_id = repo.add_budget_line(budget_id=budget.id, category_id=cat)
    repo.commit()

    win = BudgetWindow(repo)
    for _ in range(5):
        _app.processEvents()
    repo.close()

    # Would otherwise raise, or pop a modal mid-shutdown.
    assert win._on_edit_allocation(line_id, "2026-01", Decimal("10")) is False
    assert prompted == [], "the copy-forward prompt was reached after close"
