"""Offscreen-Qt smoke for the lockable API-key field (ADR-127).

Pins the load-bearing UI contract of ``LockableSecretField`` and its use in the
Securities + Currencies dialogs: a stored key opens **locked** (read-only +
Change button shown) so it can't be edited by accident; a fresh (empty) field
opens **unlocked**; the Change button unlocks it; and ``.text()`` still returns
the value through the wrapper so the refresh/save handlers read it unchanged.

Needs PySide6 + an offscreen platform — run with the miniforge python3:

    QT_QPA_PLATFORM=offscreen \
    /opt/homebrew/Caskroom/miniforge/base/bin/python3 tests/test_secret_field_smoke.py
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
from mfl_desktop.ui.secret_field import LockableSecretField
from mfl_desktop.ui.securities_dialog import SecuritiesDialog
from mfl_desktop.ui.currencies_dialog import CurrenciesDialog


def _repo() -> Repository:
    db = Path(tempfile.mkdtemp(prefix="mfl_secret_")) / "Money.mfl"
    return Repository(db)


# ── component ────────────────────────────────────────────────────────────────


def test_seeded_field_starts_locked():
    f = LockableSecretField(value="abc123")
    assert f.is_locked()
    assert f.line_edit.isReadOnly()
    assert f.text() == "abc123"


def test_empty_field_starts_unlocked():
    f = LockableSecretField(value="")
    assert not f.is_locked()
    assert not f.line_edit.isReadOnly()


def test_change_unlocks_and_preserves_text():
    f = LockableSecretField(value="secret")
    f.set_locked(False)  # simulate the Change click
    assert not f.is_locked()
    assert not f.line_edit.isReadOnly()
    assert f.text() == "secret"  # existing value stays until the user edits


def test_relock_makes_readonly_again():
    f = LockableSecretField(value="")
    assert not f.is_locked()
    f.set_locked(True)
    assert f.is_locked() and f.line_edit.isReadOnly()


# ── dialog integration ──────────────────────────────────────────────────────


def test_securities_dialog_locks_stored_key():
    repo = _repo()
    repo.set_setting("tiingo_api_key", "tiingo-token")
    dlg = SecuritiesDialog(repo)
    assert dlg._key_field.is_locked()
    assert dlg._key_edit.text() == "tiingo-token"  # handlers read _key_edit
    dlg._key_field.set_locked(False)
    assert not dlg._key_field.is_locked()


def test_securities_dialog_fresh_key_unlocked():
    repo = _repo()  # no key stored
    dlg = SecuritiesDialog(repo)
    assert not dlg._key_field.is_locked()


def test_currencies_dialog_locks_stored_key():
    repo = _repo()
    repo.set_setting("oxr_api_key", "oxr-app-id")
    dlg = CurrenciesDialog(repo)
    assert dlg._key_field.is_locked()
    assert dlg._key_edit.text() == "oxr-app-id"


def test_currencies_dialog_fresh_key_unlocked():
    repo = _repo()
    dlg = CurrenciesDialog(repo)
    assert not dlg._key_field.is_locked()


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
