"""App-session sandbox bookmark glue (ADR-125, increment B).

Pins the thin layer in ``mfl_desktop.app_session`` that wires the
``mfl_desktop.sandbox`` foundation into the cross-launch "reopen my file"
pointer: ``remember_last_db`` additionally persists a security-scoped bookmark
when sandboxed, and ``begin_main_file_access`` resolves it and *starts* access.
The sandbox primitives themselves are faked here (real ones are covered by
``test_sandbox.py``); this test owns the QSettings persistence + start()
contract.

Needs PySide6 (QSettings) — run under the miniforge interpreter:

    /opt/homebrew/Caskroom/miniforge/base/bin/python3 tests/test_app_session_sandbox.py

Hermetic: QSettings is redirected to a temp INI scope so the user's real
settings are never touched.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtCore import QCoreApplication, QSettings

# Redirect QSettings() to a throwaway INI file before importing the module under
# test, so nothing writes to the real per-user store.
_TMP = tempfile.mkdtemp(prefix="mfl_appsession_")
QCoreApplication.setOrganizationName("MFLTest")
QCoreApplication.setApplicationName("MFLTestApp")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _TMP)

from mfl_desktop import app_session, sandbox


def _reset_settings() -> None:
    QSettings().clear()


class _FakeLoc:
    def __init__(self, path):
        self.path = Path(path)
        self.started = False

    def start(self):
        self.started = True
        return True

    def stop(self):
        pass


class _PatchSandbox:
    """Swap sandbox functions for fakes, restoring them after."""

    def __init__(self, **kw):
        self._kw = kw
        self._saved = {}

    def __enter__(self):
        for k, v in self._kw.items():
            self._saved[k] = getattr(sandbox, k)
            setattr(sandbox, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(sandbox, k, v)


def _tmpfile() -> Path:
    p = Path(tempfile.mkdtemp(prefix="mfl_appsession_")) / "live.mfl"
    p.write_bytes(b"db")
    return p


# ── sandboxed ────────────────────────────────────────────────────────────────


def test_remember_writes_bookmark_when_sandboxed():
    _reset_settings()
    p = _tmpfile()
    with _PatchSandbox(
        is_sandboxed=lambda: True,
        create_security_scoped_bookmark=lambda path: "FAKE_BLOB",
    ):
        app_session.remember_last_db(p)
    s = QSettings()
    assert s.value("session/last_db_path") == str(p.resolve())
    assert s.value("session/last_db_bookmark") == "FAKE_BLOB"


def test_begin_access_resolves_and_starts():
    _reset_settings()
    p = _tmpfile()
    loc = _FakeLoc(p)
    with _PatchSandbox(
        is_sandboxed=lambda: True,
        create_security_scoped_bookmark=lambda path: "FAKE_BLOB",
        resolve_security_scoped_bookmark=lambda blob: loc if blob == "FAKE_BLOB" else None,
    ):
        app_session.remember_last_db(p)
        got = app_session.begin_main_file_access()
    assert got is loc
    assert loc.started is True       # access was begun for the caller


def test_begin_access_none_when_bookmark_unresolvable():
    _reset_settings()
    p = _tmpfile()
    with _PatchSandbox(
        is_sandboxed=lambda: True,
        create_security_scoped_bookmark=lambda path: "STALE",
        resolve_security_scoped_bookmark=lambda blob: None,   # can't resolve
    ):
        app_session.remember_last_db(p)
        got = app_session.begin_main_file_access()
    assert got is None               # → resolver falls back to plain-path/recovery


# ── unsandboxed (dev / non-mac) ──────────────────────────────────────────────


def test_remember_no_bookmark_when_unsandboxed():
    _reset_settings()
    # Pre-seed a stale bookmark to prove it gets cleared.
    QSettings().setValue("session/last_db_bookmark", "OLD")
    p = _tmpfile()
    with _PatchSandbox(is_sandboxed=lambda: False):
        app_session.remember_last_db(p)
    s = QSettings()
    assert s.value("session/last_db_path") == str(p.resolve())
    assert not s.value("session/last_db_bookmark")     # cleared / absent


def test_begin_access_none_without_bookmark():
    _reset_settings()
    p = _tmpfile()
    with _PatchSandbox(is_sandboxed=lambda: False):
        app_session.remember_last_db(p)
        assert app_session.begin_main_file_access() is None


def test_last_db_path_unchanged_contract():
    # last_db_path still returns the plain stored path (the resolver's primary
    # input), regardless of any bookmark.
    _reset_settings()
    p = _tmpfile()
    with _PatchSandbox(
        is_sandboxed=lambda: True,
        create_security_scoped_bookmark=lambda path: "FAKE_BLOB",
    ):
        app_session.remember_last_db(p)
    assert app_session.last_db_path() == p.resolve()


# ── bare-script runner ───────────────────────────────────────────────────────


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
