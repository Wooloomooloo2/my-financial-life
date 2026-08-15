"""The reconcile wizard pre-selects; it does not hide (ADR-189).

Owner-reported from two screenshots: page 1 with "Automatically select:
Nothing" and both Include boxes clear, then page 2 with an **empty table**, a
red £1,244.42 Missing, and a banner reading "10 pending transactions in this
period are not shown". There was nothing to check off — the account is kept by
hand, so every row is `pending`, and ADR-130's candidate gate had removed all
ten from the screen.

The gate was answering the right question with the wrong verb. Pre-ticking a
row the bank never confirmed is a real hazard; *listing* one is not. So the
Include controls now only decide what starts **ticked**, and the table always
shows every unreconciled row.

These drive the real ``ReconcileWizard`` offscreen — the empty-table symptom
was invisible to the repository-level tests, which asserted the gate worked.

Run: ``.venv/bin/pytest tests/test_reconcile_preselection.py``
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

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.reconcile_wizard import (
    ReconcileWizard,
    _ROLE_STATUS,
    _ROLE_TXN_ID,
)

_START = "2026-07-14"
_END = "2026-08-13"


def _build():
    """An account shaped like the report: mostly pending, plus one of each
    other status, and a pending row dated outside the statement period."""
    db = Path(tempfile.mkdtemp(prefix="mfl_presel_")) / "money.mfl"
    repo = Repository(db)
    acct = repo.create_account(
        name="Capital One UK", type_key="credit", currency="GBP",
    )
    cat = repo.create_category("Food", None, "expense")
    ids: dict[str, int] = {}

    def add(key, posted_date, status, amount):
        ids[key] = repo.insert_transaction(
            account_id=acct.id, posted_date=posted_date,
            amount=Decimal(amount), payee_id=None, category_id=cat,
            status=status, memo="", import_hash=None, import_batch_id=None,
        )

    add("pending_a", "2026-07-20", "pending", "-25.00")
    add("pending_b", "2026-08-01", "pending", "-40.00")
    add("cleared", "2026-07-25", "cleared", "-15.00")
    add("matched", "2026-07-30", "matched", "-30.00")
    add("pending_outside", "2026-09-05", "pending", "-99.00")
    repo.commit()
    return repo, acct, ids


def _wizard(repo, acct):
    w = ReconcileWizard(repo=repo, account=acct)
    w._start_date.setDate(QDate.fromString(_START, "yyyy-MM-dd"))
    w._end_date.setDate(QDate.fromString(_END, "yyyy-MM-dd"))
    w._start_balance.setText("-1546.18")
    w._end_balance.setText("-301.76")
    return w


def _listed(w) -> set[int]:
    return {
        int(w._table.item(r, 0).data(_ROLE_TXN_ID))
        for r in range(w._table.rowCount())
    }


def _ticked(w) -> set[int]:
    return {
        int(w._table.item(r, 0).data(_ROLE_TXN_ID))
        for r in range(w._table.rowCount())
        if w._table.item(r, 0).checkState() == Qt.Checked
    }


def _status_of(w, txn_id) -> str:
    for r in range(w._table.rowCount()):
        if int(w._table.item(r, 0).data(_ROLE_TXN_ID)) == txn_id:
            return str(w._table.item(r, 0).data(_ROLE_STATUS))
    raise AssertionError(f"txn {txn_id} not listed")


# ── the reported bug ────────────────────────────────────────────────────────


def test_nothing_selected_still_lists_everything():
    """The screenshot, asserted: auto-select "Nothing", both boxes clear — the
    table is fully populated and nothing is ticked. Before ADR-189 this table
    had zero rows."""
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    w._auto_combo.setCurrentIndex(w._auto_combo.findData("none"))
    w._on_next()

    assert _listed(w) == set(ids.values()), _listed(w)
    assert _ticked(w) == set()


def test_matched_mode_lists_everything_and_ticks_only_matched():
    """The default. Cleared and pending are listed but unticked — visible for
    a deliberate click, never selected on the user's behalf."""
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    w._on_next()

    assert _listed(w) == set(ids.values())
    assert _ticked(w) == {ids["matched"]}


# ── the checkboxes move ticks, never rows ───────────────────────────────────


def test_pending_toggle_ticks_without_changing_the_list():
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    w._on_next()
    before = _listed(w)

    w._include_pending_check.setChecked(True)
    assert _listed(w) == before, "the row set must not move"
    assert ids["pending_a"] in _ticked(w)
    assert ids["pending_b"] in _ticked(w)


def test_pending_toggle_off_is_an_exact_undo():
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    w._on_next()
    baseline = _ticked(w)

    w._include_pending_check.setChecked(True)
    w._include_pending_check.setChecked(False)
    assert _ticked(w) == baseline
    assert _listed(w) == set(ids.values())


def test_toggle_leaves_hand_placed_ticks_alone():
    """The pre-selection is scoped to its own status class, so flipping it does
    not wipe out a row the user ticked themselves."""
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    w._on_next()
    for r in range(w._table.rowCount()):
        if int(w._table.item(r, 0).data(_ROLE_TXN_ID)) == ids["cleared"]:
            w._table.item(r, 0).setCheckState(Qt.Checked)

    w._include_pending_check.setChecked(True)
    w._include_pending_check.setChecked(False)
    assert ids["cleared"] in _ticked(w), "hand-placed tick was clobbered"
    assert ids["matched"] in _ticked(w)


def test_preselection_is_date_bounded_but_listing_is_not():
    """The date bound survives ADR-189 — on the *ticks*. A pending row outside
    the statement period is listed (so you can tick it if the bank posted it)
    but is never pre-selected."""
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    w._on_next()
    assert ids["pending_outside"] in _listed(w)

    w._include_pending_check.setChecked(True)
    assert ids["pending_outside"] not in _ticked(w)
    assert ids["pending_a"] in _ticked(w)


def test_cleared_toggle_ticks_cleared_only():
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    w._on_next()

    w._include_cleared_check.setChecked(True)
    assert ids["cleared"] in _ticked(w)
    assert ids["pending_a"] not in _ticked(w)
    assert _listed(w) == set(ids.values())


# ── the controls agree with each other ──────────────────────────────────────


def test_boxes_are_disabled_under_nothing():
    """They extend the combo's pre-selection, so under "Nothing" they have
    nothing to extend — better disabled than live-but-inert."""
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    assert w._include_pending_check.isEnabled()

    w._auto_combo.setCurrentIndex(w._auto_combo.findData("none"))
    assert not w._include_pending_check.isEnabled()
    assert not w._include_cleared_check.isEnabled()

    w._auto_combo.setCurrentIndex(w._auto_combo.findData("matched"))
    assert w._include_pending_check.isEnabled()


def test_boxes_set_before_next_are_honoured():
    """Setting the boxes on page 1 — before the table exists — must still
    pre-select, since that is the documented order of operations."""
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    w._include_pending_check.setChecked(True)
    w._include_cleared_check.setChecked(True)
    w._on_next()

    assert _ticked(w) == {ids["matched"], ids["cleared"],
                          ids["pending_a"], ids["pending_b"]}


def test_rows_carry_their_status():
    """The tick column stores status + date so the toggles can re-tick in place
    without rebuilding the table (and losing the user's other ticks)."""
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    w._on_next()
    assert _status_of(w, ids["pending_a"]) == "pending"
    assert _status_of(w, ids["cleared"]) == "cleared"
    assert _status_of(w, ids["matched"]) == "matched"


# ── the Missing figure follows the ticks ────────────────────────────────────


def test_missing_reaches_zero_by_ticking_pending():
    """End to end: the reported account could not be reconciled at all because
    its rows were absent. Ticking the pending rows now moves Missing to zero."""
    repo, acct, ids = _build()
    w = _wizard(repo, acct)
    # Statement covering exactly the four in-period rows: 25+40+15+30 = 110 out.
    w._start_balance.setText("-1000.00")
    w._end_balance.setText("-1110.00")
    w._on_next()
    w._include_pending_check.setChecked(True)
    w._include_cleared_check.setChecked(True)

    assert "✓" in w._missing_label.text(), w._missing_label.text()
