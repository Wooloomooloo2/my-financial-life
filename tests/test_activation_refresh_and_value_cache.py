"""Activation no longer rebuilds Home, and account values are cached (ADR-153).

Three things are pinned here.

1. **The freshness token.** ``RegisterWindow.changeEvent`` refreshed Home on every
   ActivationChange — which fires when a report closes, when you switch to a
   register, on every alt-tab. That rebuild measured ~450-550ms against the live
   file, synchronously on the UI thread, and almost always redrew an identical
   dashboard. ``HomeView.refresh_if_stale()`` now compares a cheap token first.
   The ADR-075 guarantee has to survive: a *real* edit must still redraw.

2. **The account-value memo.** ``compute_account_values`` replays every investment
   account's whole ledger through the FIFO holdings engine (~360ms of the above).
   It is now memoised against ``Repository.data_generation()``. The cache must be
   invisible: same answers, invalidated by any write, keyed per-argument, and
   immune to a caller mutating the dict it gets back.

3. **The background-emit guard.** A Home background pass still in flight when the
   app is torn down used to emit from a destroyed QObject and raise
   ``RuntimeError: Signal source has been deleted`` on the worker thread. Run in a
   subprocess and judged on stderr, since the failure surfaces on a worker thread
   rather than as a raised assert.

Run headless:

    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_activation_refresh_and_value_cache.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
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
from mfl_desktop.ui.home_view import HomeView
from mfl_desktop.ui.theme import apply_theme

_DEMO = _REPO_ROOT / "mfl_public.mfl"

_INSERT_TXN = (
    "INSERT INTO txn (iri, account_id, category_id, posted_date, amount, status) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def _repo() -> Repository:
    tmp = Path(tempfile.mkdtemp(prefix="mfl_adr153_")) / "demo.mfl"
    shutil.copy(_DEMO, tmp)
    repo = Repository(tmp)
    apply_theme(_app, "light")
    return repo


def _post_txn(repo: Repository, iri: str = "mfl:txn/adr153-probe") -> None:
    """A real edit, through the repo's own connection."""
    acct = repo.connection.execute("SELECT id FROM account LIMIT 1").fetchone()[0]
    cat = repo.connection.execute("SELECT id FROM category LIMIT 1").fetchone()[0]
    repo.connection.execute(
        _INSERT_TXN, (iri, acct, cat, "2026-07-03", 4242, "cleared"),
    )
    repo.connection.commit()


# ── 1. the freshness token ──────────────────────────────────────────────────

def test_unchanged_data_does_not_rebuild_home():
    """The whole point: activation on unmoved data must be a no-op."""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()                                # first paint
    container = view._container
    assert view.refresh_if_stale() is False       # would have rebuilt
    assert view._container is container           # same widget tree, untouched


def test_a_real_edit_still_rebuilds_home():
    """ADR-075's guarantee — an edit made elsewhere shows up on activation."""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()
    container = view._container
    _post_txn(repo)
    assert view.refresh_if_stale() is True
    assert view._container is not container       # genuinely rebuilt


def test_external_write_rebuilds_home_once_declared():
    """A background worker writes on its own connection (the launch price/FX
    refreshes). It cannot be seen via our connection, so it is declared."""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()
    assert view.refresh_if_stale() is False
    repo.note_external_change()
    assert view.refresh_if_stale() is True


def test_swapping_the_file_forces_a_rebuild():
    """File ▸ Open swaps the repo. The generation counter is per-Repository, so a
    token from the old file says nothing about the new one."""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()
    assert view.refresh_if_stale() is False
    view.set_repo(_repo())                        # different file
    assert view.refresh_if_stale() is True


# ── 2. the account-value memo ───────────────────────────────────────────────

def test_cache_returns_the_same_values_as_the_uncached_path():
    repo = _repo()
    assert repo.compute_account_values() == repo._compute_account_values_uncached(
        False, None,
    )


def test_cache_is_invalidated_by_a_write_on_our_connection():
    repo = _repo()
    before = repo.compute_account_values()
    _post_txn(repo)
    assert repo.compute_account_values() != before


def test_cache_is_invalidated_by_a_declared_external_write():
    repo = _repo()
    repo.compute_account_values()
    gen = repo.data_generation()
    repo.note_external_change()
    assert repo.data_generation() != gen


def test_cache_is_keyed_per_argument():
    """as_of_date changes the answer, so it must not collide in the cache."""
    repo = _repo()
    whole_ledger = repo.compute_account_values()
    long_ago = repo.compute_account_values(as_of_date="2000-01-01")
    assert whole_ledger != long_ago
    assert repo.compute_account_values() == whole_ledger   # not clobbered


def test_caller_cannot_poison_the_cache():
    """Callers treat the result as theirs. They get a copy."""
    repo = _repo()
    got = repo.compute_account_values()
    victim = next(iter(got))
    got[victim] = "corrupted"
    assert repo.compute_account_values()[victim] != "corrupted"


# ── 3. the background-emit guard ────────────────────────────────────────────

_SHUTDOWN_RACE = """
import sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication(sys.argv)
sys.path.insert(0, {root!r})
from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.home_view import HomeView
repo = Repository(Path({db!r}))
view = HomeView(repo)
view.setAttribute(Qt.WA_DontShowOnScreen, True)
view.show()
view.refresh()          # kicks off the background pass
del view
app.quit(); del app     # tear down with the worker still running
"""


def test_shutdown_while_a_background_pass_is_in_flight_is_quiet():
    """Quitting mid-pass must not raise on the worker thread.

    Ownership of the signals object is not enough on its own — at shutdown Qt
    destroys the C++ object regardless of who holds the Python reference — so the
    emit is guarded. Asserted on stderr because the RuntimeError is raised inside
    a QRunnable override on a worker thread, where pytest cannot see it.
    """
    repo = _repo()
    db = str(repo.db_path)
    repo.close()
    proc = subprocess.run(
        [sys.executable, "-c",
         textwrap.dedent(_SHUTDOWN_RACE).format(root=str(_REPO_ROOT), db=db)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert "Signal source has been deleted" not in proc.stderr, proc.stderr
    assert "QRunnable::run" not in proc.stderr, proc.stderr


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
