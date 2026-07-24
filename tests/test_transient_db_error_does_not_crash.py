"""A transient SQLite error on activation must not take the app down (ADR-179).

On 2026-07-24 an app left open overnight raised, three times in 44 seconds::

    File "mfl_desktop/ui/register_window.py", line 608, in changeEvent
      self._home_view.refresh_if_stale()
    ...
    File "mfl_desktop/db/repository.py", line 919, in data_generation
      self._conn.execute("PRAGMA data_version").fetchone()[0],
    sqlite3.OperationalError: locking protocol

``PRAGMA data_version`` is a *cache-invalidation hint* — "is it worth redoing the
work?" — read from a *window-activation handler*. Neither is a place worth dying
in, and the three-in-a-row is the tell: the crash dialog re-activates the window
when dismissed, which re-runs the handler, which crashes again.

Two behaviours are pinned:

1. **The probe degrades, it does not raise.** It retries briefly (SQLITE_PROTOCOL
   is a transient lock-contention result), and if it still cannot read, it
   reports a token that differs from the last one — "assume the data moved" —
   so callers redo their work instead of trusting a stale cache.
2. **Ambient refreshes swallow database errors.** ``refresh_if_stale`` and the
   window's ``changeEvent`` return quietly, leaving the last good render up. A
   user-*initiated* refresh still fails loudly.

Run headless:

    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_transient_db_error_does_not_crash.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.home_view import HomeView
from mfl_desktop.ui.register_window import RegisterWindow
from mfl_desktop.ui.theme import apply_theme

_DEMO = _REPO_ROOT / "mfl_public.mfl"


def _repo() -> Repository:
    tmp = Path(tempfile.mkdtemp(prefix="mfl_adr179_")) / "demo.mfl"
    shutil.copy(_DEMO, tmp)
    apply_theme(_app, "light")
    return Repository(tmp)


class _FlakyConn:
    """A real connection that fails ``PRAGMA data_version`` the first ``fails``
    times it is asked, exactly as SQLITE_PROTOCOL did. Everything else — other
    statements, ``total_changes``, ``commit`` — passes straight through, because
    the observed failure was *partial*: the queries the activation handler ran
    immediately beforehand all succeeded."""

    def __init__(self, real: sqlite3.Connection, fails: int) -> None:
        self._real = real
        self.remaining = fails
        self.attempts = 0

    def execute(self, sql, *args, **kwargs):
        if "data_version" in sql:
            self.attempts += 1
            if self.remaining > 0:
                self.remaining -= 1
                raise sqlite3.OperationalError("locking protocol")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _make_flaky(repo: Repository, fails: int) -> _FlakyConn:
    conn = _FlakyConn(repo._conn, fails)
    repo._conn = conn
    return conn


# ── 1. the probe degrades ───────────────────────────────────────────────────

def test_a_momentary_failure_is_retried_and_invisible():
    """One blip is what SQLite's own WAL-index contention looks like. Retrying
    absorbs it, so nothing downstream ever hears about it."""
    repo = _repo()
    healthy = repo.data_generation()
    conn = _make_flaky(repo, fails=1)
    assert repo.data_generation() == healthy      # same token, no degradation
    assert conn.attempts == 2                     # it really did retry


def test_a_persistent_failure_does_not_raise():
    """The crash itself: this call is a hint, and it used to be fatal."""
    repo = _repo()
    _make_flaky(repo, fails=99)
    repo.data_generation()                        # must not raise


def test_a_persistent_failure_reports_the_data_as_moved():
    """Failing safe means failing *stale-side*: an unreadable hint has to look
    like a change, or callers keep serving a cache they can no longer vouch
    for. Two consecutive degraded reads must also differ from each other."""
    repo = _repo()
    before = repo.data_generation()
    _make_flaky(repo, fails=99)
    first = repo.data_generation()
    second = repo.data_generation()
    assert first != before
    assert second != first


def test_the_expensive_memo_recomputes_rather_than_serving_a_stale_cache():
    """``compute_account_values`` is keyed on the token (ADR-156). A degraded
    token must miss the cache, not crash it — and still return real numbers."""
    repo = _repo()
    healthy = repo.compute_account_values()
    _make_flaky(repo, fails=99)
    assert repo.compute_account_values() == healthy


def test_it_recovers_when_the_database_settles():
    """Degradation is not a latch. Once the pragma reads again, the token goes
    back to being stable and the no-op activation is free again."""
    repo = _repo()
    _make_flaky(repo, fails=2)
    repo.data_generation()                        # degraded (2 tries, 2 fails)
    settled = repo.data_generation()
    assert repo.data_generation() == settled      # stable once more


# ── 2. ambient refreshes swallow it ─────────────────────────────────────────

class _ProbeOnly:
    """A connection on which ``is_open()``'s probe still passes but real work
    fails — the shape of the incident, and the reason the probe did not save us.

    ``is_open()`` asks ``SELECT 1``, which SQLite answers from the parser without
    touching the database file: no page read, no WAL-index lock, nothing that
    ``locking protocol`` could break. So it returned True for a connection that
    could not read a row, and the caller walked straight into the failure. That
    is why the guard has to be a real ``except``, not a pre-flight check."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql, *args, **kwargs):
        if str(sql).strip().upper() == "SELECT 1":
            return self._real.execute(sql, *args, **kwargs)
        raise sqlite3.OperationalError("locking protocol")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_the_probe_that_should_have_caught_this_does_not():
    """Pinned so the next reader does not 'simplify' the guard away in favour of
    ``is_open()``. It says yes to a connection that cannot read a row."""
    repo = _repo()
    repo._conn = _ProbeOnly(repo._conn)
    assert repo.is_open() is True
    try:
        repo.list_accounts()
    except sqlite3.Error:
        pass
    else:
        raise AssertionError("expected the real query to fail")


def test_activation_refresh_keeps_the_last_good_render():
    """Nobody asked for this refresh. The honest outcome is the dashboard that
    is already on screen, not a crash dialog.

    (``refresh()`` already swallowed a failed *gather* before this change — the
    hole was the token probe in front of it, which is why the guard added here
    is defence in depth rather than the fix. Both are pinned: what matters to
    the user is that the widget tree survives.)"""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()
    container = view._container
    repo._conn = _ProbeOnly(repo._conn)
    view.refresh_if_stale()                        # must not raise
    assert view._container is container            # last good render still up


def test_the_incident_shape_does_not_raise():
    """Only ``PRAGMA data_version`` fails; everything else works — precisely
    what the traceback showed. This is the crash, reproduced."""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()
    _make_flaky(repo, fails=99)
    view.refresh_if_stale()                        # must not raise


def test_the_window_survives_activation_on_a_broken_database():
    """End to end: the exact event that crashed. Three times, because the real
    incident's second and third crashes came from dismissing the dialog raised
    by the first."""
    repo = _repo()
    win = RegisterWindow(repo, None)
    win.show()
    for _ in range(12):
        _app.processEvents()
    assert win.isActiveWindow()                    # or the handler no-ops
    assert win._main_stack.currentIndex() == 0     # ...and Home is showing
    repo._conn = _ProbeOnly(repo._conn)
    for _ in range(3):
        win.changeEvent(QEvent(QEvent.ActivationChange))   # must not raise
    win.close()


# ── bare-script runner ──────────────────────────────────────────────────────

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001 — any raise here is the bug
            failures += 1
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
