"""Offscreen-Qt smoke for the ADR-112 category-map UI.

Builds the changed CategoriesDialog and the new ImportMappingsDialog against a
real temp ``.mfl`` and asserts the load-bearing contracts:

- CategoriesDialog's "Match imports only" checkbox reflects and writes the
  setting both ways;
- CategoriesDialog opens ImportMappingsDialog without error;
- ImportMappingsDialog lists recorded mappings and Forget removes one.

Needs PySide6 + an offscreen platform — run with the miniforge python3:

    QT_QPA_PLATFORM=offscreen \
    /opt/homebrew/Caskroom/miniforge/base/bin/python3 tests/test_category_map_dialogs_smoke.py
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

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.categories_dialog import CategoriesDialog
from mfl_desktop.ui.import_mappings_dialog import ImportMappingsDialog


def _repo() -> Repository:
    db = Path(tempfile.mkdtemp(prefix="mfl_catmap_dlg_")) / "Money.mfl"
    return Repository(db)


def test_match_only_checkbox_round_trips():
    repo = _repo()
    dlg = CategoriesDialog(repo)
    assert dlg._match_only_chk.isChecked() is False  # default off
    dlg._match_only_chk.setChecked(True)
    assert repo.import_match_only_categories() is True
    dlg._match_only_chk.setChecked(False)
    assert repo.import_match_only_categories() is False


def test_match_only_checkbox_reflects_existing_setting():
    repo = _repo()
    repo.set_import_match_only_categories(True)
    dlg = CategoriesDialog(repo)
    assert dlg._match_only_chk.isChecked() is True


def test_mappings_dialog_lists_and_forgets():
    repo = _repo()
    target = repo.find_or_create_category_path(["Bills", "Cable and Internet"])
    repo.set_category_import_mapping("Bills:Utilities:Cable", target)

    dlg = ImportMappingsDialog(repo)
    assert dlg._tree.topLevelItemCount() == 1
    item = dlg._tree.topLevelItem(0)
    assert item.text(0) == "bills:utilities:cable"
    assert "Cable and Internet" in item.text(1)

    # Forget it directly (bypassing the modal confirm) and reload.
    repo.delete_category_import_mapping("Bills:Utilities:Cable")
    dlg._reload()
    assert dlg._tree.topLevelItemCount() == 0
    # isVisibleTo ignores that the dialog itself is never shown offscreen.
    assert dlg._empty_lbl.isVisibleTo(dlg) is True
    assert dlg._tree.isVisibleTo(dlg) is False


def test_categories_dialog_opens_mappings_without_error():
    repo = _repo()
    dlg = CategoriesDialog(repo)
    # The handler constructs and exec()s a modal; build the child directly to
    # avoid blocking on exec() under offscreen.
    child = ImportMappingsDialog(repo, dlg)
    assert child is not None


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
