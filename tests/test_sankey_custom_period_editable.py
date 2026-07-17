"""A saved Sankey report's custom date range stays editable (ADR-175).

Owner-reported: create a report with a custom date range, and afterwards you
can't edit that range.

It was specific to the Sankey (Cash Flow) report, the only one that edits its
period through a **toolbar `QComboBox`** whose "Custom…" item opens a dialog.
Every other report edits its period inside the Filter dialog (where the range
is always live), and Account Summary / the register drill use *checkable
buttons* (whose `clicked` fires even when already selected). Only the combo has
the trap: it was wired to `currentIndexChanged`, which by design does not emit
when the selection doesn't change — and a saved custom report loads with the
combo already on "Custom…", so re-picking it to edit the range was silent.

The fix wires it to `activated` (fires on every user pick, current item
included) and always opens the editor on a custom pick, seeded from the stored
range.

What these lock down:

- Re-picking "Custom…" while already on custom **opens the editor** (the bug).
- The editor is **seeded from the saved range**, not from a preset's bounds.
- The edited range is applied.
- Re-picking the **same non-custom preset** does not dirty the report — a
  no-op `activated` emits where `currentIndexChanged` used to stay silent.
- Switching to a different preset still works.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_sankey_custom_period_editable.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication, QDialog

_app = QApplication.instance() or QApplication([])

import mfl_desktop.ui.sankey_report_window as M
from mfl_desktop.db.repository import Repository
from mfl_desktop.reports.filters import SankeyFilters, TYPE_SANKEY


class _FakeCustomDialog:
    """Stands in for CustomPeriodDialog: records what it was seeded with, and
    accepts with a fixed edited range."""

    seen: tuple = ()
    accept = True
    result = (date(2026, 3, 1), date(2026, 3, 31))

    def __init__(self, *, initial_from, initial_to, parent=None) -> None:
        _FakeCustomDialog.seen = (initial_from, initial_to)

    def exec(self) -> int:
        return QDialog.Accepted if _FakeCustomDialog.accept else QDialog.Rejected

    def values(self):
        return _FakeCustomDialog.result


def _window(period_key, *, custom_start=None, custom_end=None):
    db = Path(tempfile.mkdtemp(prefix="mfl_skcustom_")) / "m.mfl"
    repo = Repository(db)
    repo.create_account(name="Cur", type_key="cash", currency="GBP",
                        opening_balance=Decimal("100"))
    f = replace(SankeyFilters.default(), period_key=period_key,
                custom_start=custom_start, custom_end=custom_end)
    r = repo.create_report(name="Flow", type_key=TYPE_SANKEY,
                           filters_json=f.to_json(), folder_id=None)
    win = M.SankeyReportWindow(repo, report=repo.get_report(r.id))
    return win


def _pick(win, key):
    """Simulate the user choosing ``key`` from the period combo — the way the
    real popup does, via the `activated` signal (not a programmatic
    setCurrentIndex, which fires no user signal)."""
    combo = win._period_combo
    combo.setCurrentIndex(combo.findData(key))
    combo.activated.emit(combo.findData(key))
    _app.processEvents()


def test_repicking_custom_while_on_custom_opens_the_editor() -> None:
    """The bug: the combo loads already on Custom, so re-choosing it to edit
    the range must still open the dialog."""
    _FakeCustomDialog.seen = ()
    M.CustomPeriodDialog = _FakeCustomDialog
    win = _window("custom", custom_start="2026-02-01", custom_end="2026-05-31")
    assert win._period_combo.currentData() == "custom", "should load on custom"

    _pick(win, "custom")
    assert _FakeCustomDialog.seen, "the custom editor never opened — the bug"


def test_the_editor_is_seeded_from_the_saved_range() -> None:
    """Not from the preset bounds `_resolve_bounds` would compute — the user is
    editing *these* dates."""
    _FakeCustomDialog.seen = ()
    M.CustomPeriodDialog = _FakeCustomDialog
    win = _window("custom", custom_start="2026-02-01", custom_end="2026-05-31")
    _pick(win, "custom")
    assert _FakeCustomDialog.seen == (date(2026, 2, 1), date(2026, 5, 31))


def test_the_edited_range_is_applied() -> None:
    _FakeCustomDialog.accept = True
    _FakeCustomDialog.result = (date(2026, 3, 1), date(2026, 3, 31))
    M.CustomPeriodDialog = _FakeCustomDialog
    win = _window("custom", custom_start="2026-02-01", custom_end="2026-05-31")
    _pick(win, "custom")
    assert win._current_filters.custom_start == "2026-03-01"
    assert win._current_filters.custom_end == "2026-03-31"


def test_cancelling_the_editor_keeps_the_old_range() -> None:
    _FakeCustomDialog.accept = False
    M.CustomPeriodDialog = _FakeCustomDialog
    win = _window("custom", custom_start="2026-02-01", custom_end="2026-05-31")
    _pick(win, "custom")
    assert win._current_filters.custom_start == "2026-02-01"
    assert win._current_filters.custom_end == "2026-05-31"
    _FakeCustomDialog.accept = True   # restore for later tests


def test_repicking_the_same_preset_does_not_dirty() -> None:
    """`activated` fires on a no-change re-pick where `currentIndexChanged` did
    not, so the handler must guard it — else clicking the current timeframe
    marks a clean report dirty for nothing."""
    win = _window("ytd")
    win._dirty = False
    _pick(win, "ytd")
    assert win._dirty is False


def test_switching_to_a_different_preset_still_works() -> None:
    win = _window("ytd")
    win._dirty = False
    _pick(win, "6m")
    assert win._current_filters.period_key == "6m"
    assert win._dirty is True


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
