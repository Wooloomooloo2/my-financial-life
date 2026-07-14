"""The register never scrolls Amount/Balance off the right edge (ADR-162).

Amount and Balance are the last columns, so *any* horizontal overflow lands on
them first — and the old fixed widths overflowed a default window by 193–243 px,
which put the ledger's two headline numbers behind a horizontal scrollbar. Memo
now stretches to absorb the slack.

**Sizing note, learned the hard way.** Under ``QT_QPA_PLATFORM=offscreen`` a
top-level window *ignores* ``resize()`` — the register came back with a 1607 px
viewport whatever we asked for, so an earlier version of these tests could never
observe a clipped column and passed against the unfixed code. Resizing the
**table widget itself** is honoured. So these drive `_table.resize(...)` to a
known viewport and assert on `QHeaderView.length()` (the total width the columns
actually demand) against it. Geometry, not pixels of text — deterministic.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication, QHeaderView

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.register_window import RegisterWindow
from mfl_desktop.ui.theme import apply_theme

_DEMO = _REPO_ROOT / "mfl_public.mfl"

# The register's viewport in the app's default 1320 px window (measured), and in
# a maximised one. The 11-column investment register cannot fit the former.
_DEFAULT_VIEWPORT = 967
_WIDE_VIEWPORT = 1225


def _win() -> RegisterWindow:
    tmp = Path(tempfile.mkdtemp(prefix="mfl_cols_")) / "demo.mfl"
    shutil.copy(_DEMO, tmp)
    apply_theme(_app, "light")
    win = RegisterWindow(Repository(tmp), None)
    win.show()
    _settle()
    return win


def _settle(n: int = 12) -> None:
    for _ in range(n):
        _app.processEvents()


def _clips_at(win: RegisterWindow, viewport: int) -> bool:
    """True when the columns demand more width than ``viewport`` — i.e. the
    right-hand end (Amount, then Balance) is unreachable without scrolling."""
    win._table.resize(viewport, 600)
    _settle(8)
    return win._table.horizontalHeader().length() > win._table.viewport().width()


def _first_investment(win: RegisterWindow):
    return next(a for a in win._repo.list_accounts() if a.family == "investment")


# ── the two registers you live in ──────────────────────────────────────────

def test_account_register_does_not_clip_at_default_width():
    win = _win()
    win._show_account("mrl:CashAccount_1")
    _settle()
    assert not _clips_at(win, _DEFAULT_VIEWPORT)


def test_all_transactions_does_not_clip_at_default_width():
    # The worse of the two: it adds an Account column, so it overflowed by
    # 243 px — putting Amount, the last column, fully off-screen.
    win = _win()
    win._show_all_transactions()
    _settle()
    assert not _clips_at(win, _DEFAULT_VIEWPORT)


# ── memo is the column that gives way ──────────────────────────────────────

def test_memo_is_the_stretch_column_and_the_money_is_not():
    win = _win()
    win._show_account("mrl:CashAccount_1")
    _settle()
    header = win._table.horizontalHeader()
    names = [name for _, name, _ in win._model.COLUMNS]
    modes = {n: header.sectionResizeMode(i) for i, n in enumerate(names)}
    assert modes["memo"] == QHeaderView.Stretch
    # The money keeps a fixed, user-draggable width — it must never be the
    # column that absorbs a narrow window.
    assert modes["amount"] == QHeaderView.Interactive
    assert modes["running_balance"] == QHeaderView.Interactive


def test_switching_register_modes_does_not_leave_a_stale_stretched_column():
    """The ordering bug this fix nearly shipped with.

    ``setColumnWidth`` is silently ignored on a section still in Stretch mode.
    Memo sits at a different index in each register mode, so switching modes
    without first resetting every section to Interactive left whichever column
    now occupies the old Stretch index (e.g. Quantity) stuck at Memo's stretched
    width — and the grid overflowed again, clipping the money.
    """
    win = _win()
    win._show_account("mrl:CashAccount_1")          # memo at one index
    _settle()
    win._show_account(_first_investment(win).iri)   # memo at another
    _settle()
    assert not _clips_at(win, _WIDE_VIEWPORT)

    widths = {
        name: win._table.columnWidth(i)
        for i, (_, name, _) in enumerate(win._model.COLUMNS)
    }
    # Quantity must be its own default, not Memo's leftover stretch width.
    assert widths["quantity"] < 200


# ── the 11-column register, stated honestly ────────────────────────────────

def test_investment_register_fits_once_wide_enough():
    """Eleven columns do not fit a 967 px viewport at any honest width, so the
    investment register still scrolls in a small window — but it must fit a
    normally-sized one. Pins the boundary rather than pretending it isn't there.
    """
    win = _win()
    win._show_account(_first_investment(win).iri)
    _settle()
    assert not _clips_at(win, _WIDE_VIEWPORT)
