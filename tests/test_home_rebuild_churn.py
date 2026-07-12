"""Home stops rebuilding itself five times at launch (ADR-157).

ADR-153 guarded the *activation* path, but two other callers still rebuilt Home
unconditionally, and instrumenting a real launch against the live file caught it:

    [2.12s]  206.5 ms  HomeView.refresh  (FULL REBUILD)   <- _show_home
    [2.27s]  142.4 ms  HomeView.refresh  (FULL REBUILD)   <- _on_bg_ready
    [3.09s]  425.8 ms  HomeView.refresh  (FULL REBUILD)   <- _show_home again
    [3.31s]  217.2 ms  HomeView.refresh  (FULL REBUILD)   <- UI FROZE 797 ms
    [3.74s]  179.6 ms  HomeView.refresh  (FULL REBUILD)

Two causes:
  * `_show_home()` rebuilt unconditionally, and it sits on the sidebar's
    selection path — which fires more than once during startup.
  * `_on_bg_ready()` re-ran the ENTIRE query pass (gather_home_data is ~85% of a
    refresh) just to fold a sparkline into a dashboard it had itself just drawn.

Pinned here: navigating to Home on unchanged data doesn't rebuild; the
background-cards callback redraws without re-querying; and neither shortcut can
serve stale data — a real edit still re-gathers, and swapping the file drops
everything.

Run headless:  QT_QPA_PLATFORM=offscreen python -m pytest tests/test_home_rebuild_churn.py
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

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

import mfl_desktop.ui.home_view as HV
from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.home_view import HomeView
from mfl_desktop.ui.theme import apply_theme

_DEMO = _REPO_ROOT / "mfl_public.mfl"


def _repo() -> Repository:
    tmp = Path(tempfile.mkdtemp(prefix="mfl_churn_")) / "demo.mfl"
    shutil.copy(_DEMO, tmp)
    repo = Repository(tmp)
    apply_theme(_app, "light")
    return repo


class _GatherCounter:
    """Counts calls to gather_home_data — the expensive ~85% of a refresh."""

    def __enter__(self):
        self.n = 0
        self._orig = HV.gather_home_data

        def counted(*a, **kw):
            self.n += 1
            return self._orig(*a, **kw)

        HV.gather_home_data = counted
        return self

    def __exit__(self, *exc):
        HV.gather_home_data = self._orig


def _post_txn(repo: Repository) -> None:
    acct = repo.connection.execute("SELECT id FROM account LIMIT 1").fetchone()[0]
    cat = repo.connection.execute("SELECT id FROM category LIMIT 1").fetchone()[0]
    repo.connection.execute(
        "INSERT INTO txn (iri, account_id, category_id, posted_date, amount, status)"
        " VALUES (?,?,?,?,?,?)",
        ("mfl:txn/churn", acct, cat, "2026-07-03", 4242, "cleared"),
    )
    repo.connection.commit()


# ── _on_bg_ready must not re-query ──────────────────────────────────────────

def test_background_cards_redraw_without_requerying():
    """The whole point: fold in the sparkline, don't re-run the query pass."""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()                       # first paint (one gather)
    container = view._container

    with _GatherCounter() as g:
        view.refresh(reuse_data=True)
        assert g.n == 0, "reuse_data must not call gather_home_data"

    assert view._container is not container   # it did redraw


def test_reuse_data_still_regathers_if_the_data_moved():
    """The shortcut must never serve stale numbers: if a write landed while the
    worker was out, re-gather rather than redraw the old data."""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()

    _post_txn(repo)                      # data moves under the worker

    with _GatherCounter() as g:
        view.refresh(reuse_data=True)
        assert g.n == 1, "a moved token must force a re-gather"


def test_reuse_data_regathers_when_there_is_nothing_cached():
    repo = _repo()
    view = HomeView(repo)
    with _GatherCounter() as g:
        view.refresh(reuse_data=True)    # nothing cached yet
        assert g.n == 1


def test_swapping_the_file_drops_the_cached_data():
    """File ▸ Open. The cached HomeData belongs to the old file and must never
    be redrawn against the new one."""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()
    assert view._last_data is not None

    view.set_repo(_repo())               # a different file
    assert view._last_data is None
    with _GatherCounter() as g:
        view.refresh(reuse_data=True)
        assert g.n == 1                  # must re-gather for the new file


# ── navigating to Home must not rebuild unchanged data ──────────────────────

def test_navigating_to_home_on_unchanged_data_does_not_rebuild():
    """_show_home sits on the sidebar's selection path, which fires repeatedly.
    On unchanged data it must be a no-op."""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()
    container = view._container

    with _GatherCounter() as g:
        assert view.refresh_if_stale() is False
        assert g.n == 0
    assert view._container is container   # untouched widget tree


def test_navigating_to_home_after_an_edit_does_rebuild():
    """The guarantee that must survive: a real edit still shows up."""
    repo = _repo()
    view = HomeView(repo)
    view.refresh()
    _post_txn(repo)
    with _GatherCounter() as g:
        assert view.refresh_if_stale() is True
        assert g.n == 1


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
