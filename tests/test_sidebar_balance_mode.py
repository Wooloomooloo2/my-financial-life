"""Today vs projected sidebar balance (ADR-131).

The left-panel balances summed the whole ledger, so future-dated ("forwarded")
transactions inflated/deflated the figure. A Today | Projected toggle now lets
the sidebar show the actual balance as of today (posted on/before today) or the
projected whole-ledger balance.

Repo behaviour is Qt-free; the toggle test needs offscreen Qt + manages its own
QSettings key so it doesn't depend on (or pollute) the machine's saved choice.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.db.repository import Repository


def _repo():
    db = Path(tempfile.mkdtemp(prefix="mfl_bal_")) / "m.mfl"
    repo = Repository(db)
    acct = repo.create_account(
        name="Cur", type_key="cash", currency="GBP",
        opening_balance=Decimal("100.00"),
    )
    cat = repo.create_category("Food", None, "expense")
    today = date.today().isoformat()
    future = (date.today() + timedelta(days=10)).isoformat()
    mk = lambda d, a: repo.insert_transaction(
        account_id=acct.id, posted_date=d, amount=Decimal(a), payee_id=None,
        category_id=cat, status="cleared", memo="", import_hash=None,
        import_batch_id=None,
    )
    mk(today, "-10.00")
    mk(future, "-50.00")          # a future-dated ("forwarded") row
    return repo, acct.id, today


# ── repository ───────────────────────────────────────────────────────────────


def test_today_excludes_future_projected_includes_it():
    repo, aid, today = _repo()
    projected = repo.compute_account_balances(include_closed=True)
    todays = repo.compute_account_balances(include_closed=True, as_of_date=today)
    assert projected[aid] == Decimal("40.00")     # 100 − 10 − 50 (future)
    assert todays[aid] == Decimal("90.00")        # 100 − 10 only


def test_account_values_honours_as_of():
    repo, aid, today = _repo()
    assert repo.compute_account_values(include_closed=True)[aid] == Decimal("40.00")
    assert repo.compute_account_values(
        include_closed=True, as_of_date=today)[aid] == Decimal("90.00")


# ── sidebar toggle ───────────────────────────────────────────────────────────


def test_sidebar_toggle_default_persist_and_signal():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QSettings
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("MFL")
    app.setApplicationName("MFL")
    from mfl_desktop.ui.sidebar import Sidebar

    key = Sidebar._BALANCE_MODE_KEY
    saved = QSettings().value(key)                 # preserve the real choice
    QSettings().remove(key)                        # unset → default 'today'
    try:
        repo, aid, today = _repo()
        acct = repo.list_accounts()[0]
        sb = Sidebar([acct], [], {aid: Decimal("90.00")}, repo=repo)
        assert sb.balance_mode() == "today"        # default

        seen = []
        sb.balance_mode_changed.connect(seen.append)
        sb._on_balance_mode_clicked("projected")
        assert sb.balance_mode() == "projected"
        assert seen == ["projected"]
        assert Sidebar.saved_balance_mode() == "projected"   # persisted

        # clicking the already-active mode is a no-op (no extra signal)
        sb._on_balance_mode_clicked("projected")
        assert seen == ["projected"]
    finally:
        if saved is None:
            QSettings().remove(key)
        else:
            QSettings().setValue(key, saved)


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
