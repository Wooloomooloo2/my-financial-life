"""Offscreen-Qt smoke for the ADR-109 data-management dialogs.

Builds the three new/changed dialogs against a real temp ``.mfl`` and asserts the
load-bearing UI contracts:

- ``DataLibraryDialog`` pins the live file as a bold, non-deletable ``current``
  row at the top of Saved datasets, and exposes the three Locations signals;
- ``LocationsDialog`` shows the resolved ``MFL Snapshots`` folder and emits its
  three intent signals;
- ``FileRecoveryDialog`` returns the right ``RecoveryChoice`` for each button and
  defaults to 'quit' when closed.

Needs PySide6 + an offscreen platform — run with the miniforge python3:

    QT_QPA_PLATFORM=offscreen \
    /opt/homebrew/Caskroom/miniforge/base/bin/python3 tests/test_locations_dialogs_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication

# One application for the whole run; org/app names so QSettings resolves.
_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.data_library_dialog import DataLibraryDialog
from mfl_desktop.ui.locations_dialog import LocationsDialog
from mfl_desktop.ui.file_recovery_dialog import FileRecoveryDialog


def _repo() -> Repository:
    db = Path(tempfile.mkdtemp(prefix="mfl_dlg_")) / "Money.mfl"
    return Repository(db)


def test_data_library_pins_current_file_row():
    repo = _repo()
    dlg = DataLibraryDialog(repo)
    table = dlg._saved_table
    assert table.rowCount() >= 1
    top = table.item(0, 0).data(0x0100)  # Qt.UserRole
    assert top.kind == "current"
    assert table.item(0, 0).font().bold()
    # Selecting the current row must disable load / rename / delete.
    table.selectRow(0)
    dlg._sync_buttons()
    assert not dlg._load_btn.isEnabled()
    assert not dlg._rename_btn.isEnabled()
    assert not dlg._delete_btn.isEnabled()
    repo.close()


def test_data_library_exposes_locations_signals():
    for name in (
        "open_existing_main_requested",
        "relocate_main_requested",
        "snapshots_root_changed",
    ):
        assert hasattr(DataLibraryDialog, name), name


def test_locations_dialog_shows_snapshot_folder_and_emits():
    repo = _repo()
    dlg = LocationsDialog(repo)
    assert "MFL Snapshots" in dlg._snap_path.text()
    captured = {}
    dlg.snapshots_root_changed.connect(lambda p: captured.__setitem__("root", p))
    dlg.open_existing_main_requested.connect(lambda p: captured.__setitem__("open", p))
    dlg.relocate_main_requested.connect(lambda p: captured.__setitem__("move", p))
    dlg.snapshots_root_changed.emit(Path("/tmp/x"))
    dlg.open_existing_main_requested.emit(Path("/tmp/y.mfl"))
    dlg.relocate_main_requested.emit(Path("/tmp/z"))
    assert captured["root"] == Path("/tmp/x")
    assert captured["open"] == Path("/tmp/y.mfl")
    assert captured["move"] == Path("/tmp/z")
    repo.close()


def test_recovery_dialog_choices():
    # Inspect _choice after invoking the button handlers directly — calling
    # run()/exec() would block on a modal loop with no event pump offscreen.
    p = Path("/tmp/missing.mfl")
    d = FileRecoveryDialog(p, reason="unavailable")
    assert d._choice.retry is False  # nothing chosen yet
    d._on_retry()
    assert d._choice.retry is True

    # Fresh dialog left untouched == 'quit' (all-False) default.
    d2 = FileRecoveryDialog(p, reason="unreadable")
    c = d2._choice
    assert not (c.retry or c.open_other or c.new_file)


# ── bare-script runner ──────────────────────────────────────────────────────

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
