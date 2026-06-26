"""Secondary windows survive a shutdown-time activate-refresh (ADR-109 follow-up).

On quit, the RegisterWindow closes the shared Repository. A secondary window
(Account Summary, a drill-down list) can still receive a queued ``WindowActivate``
*after* that close; its refresh then queried the closed connection and crashed
the quit with ``sqlite3.ProgrammingError: Cannot operate on a closed database``.

Both windows now guard their activate-refresh on ``repo.is_open()`` (the same
guard BudgetWindow / HomeView already use). This pins that.

Needs PySide6 + offscreen — run under the miniforge python3:

    QT_QPA_PLATFORM=offscreen \
    /opt/homebrew/Caskroom/miniforge/base/bin/python3 \
    tests/test_shutdown_refresh_guard.py
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

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.theme import apply_theme
from mfl_desktop.ui.account_summary_window import AccountSummaryWindow
from mfl_desktop.ui.transactions_list_window import (
    TransactionsListWindow,
    TxnListFilter,
)

_DEMO = _REPO_ROOT / "mfl_public.mfl"


def _repo() -> Repository:
    tmp = Path(tempfile.mkdtemp(prefix="mfl_shut_")) / "demo.mfl"
    shutil.copy(_DEMO, tmp)
    repo = Repository(tmp)
    apply_theme(_app, "light")
    return repo


def test_account_summary_reload_after_close_is_safe():
    repo = _repo()
    acct = repo.connection.execute("SELECT id FROM account LIMIT 1").fetchone()[0]
    win = AccountSummaryWindow(repo, acct)
    repo.close()                                   # simulate shutdown order
    win.reload()                                   # must not raise
    _app.sendEvent(win, QEvent(QEvent.WindowActivate))  # nor via the event path


def test_drilldown_activate_after_close_is_safe():
    repo = _repo()
    acct = repo.connection.execute("SELECT id FROM account LIMIT 1").fetchone()[0]
    flt = TxnListFilter.for_category(
        account_id=acct, account_name="x", category_id=None,
        category_label="x", period_key="1y",
    )
    win = TransactionsListWindow(repo, flt)
    repo.close()
    _app.sendEvent(win, QEvent(QEvent.WindowActivate))  # must not raise


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
