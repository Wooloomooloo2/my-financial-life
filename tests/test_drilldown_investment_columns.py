"""Investment drill-downs use the security-aware column layout (ADR-109 follow-up).

Regression guard for the bug where drilling from an investment report
(Investment Income / Returns, or an investment Account Summary) into the
transaction list opened a register showing the *cash* columns (Payee, Category)
instead of the investment columns (Action, Symbol, Security, Qty, Price).

The fix: ``TransactionTableModel`` selects ``COLUMNS_INVEST`` (single account) or
``COLUMNS_INVEST_ALL`` (cross-account security drill) when told ``invest=True``,
and ``TransactionsListWindow`` now derives that flag from the account family /
security scope instead of always defaulting to cash columns.

Needs PySide6 (the table model is a ``QAbstractTableModel``); run offscreen under
the miniforge python3:

    QT_QPA_PLATFORM=offscreen \
    /opt/homebrew/Caskroom/miniforge/base/bin/python3 \
    tests/test_drilldown_investment_columns.py
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
from mfl_desktop.ui.register_model import TransactionTableModel


def _repo() -> Repository:
    return Repository(Path(tempfile.mkdtemp(prefix="mfl_cols_")) / "f.mfl")


def _cols(model) -> list[str]:
    return [name for (_, name, _) in model.COLUMNS]


def test_single_investment_account_uses_invest_columns():
    m = TransactionTableModel(_repo(), account_id=5, invest=True)
    cols = _cols(m)
    assert "security_name" in cols and "action" in cols and "price" in cols
    assert "payee_name" not in cols


def test_cross_account_security_drill_uses_invest_all_columns():
    m = TransactionTableModel(_repo(), account_id=None, invest=True)
    cols = _cols(m)
    assert m.COLUMNS is m.COLUMNS_INVEST_ALL
    assert "account_name" in cols           # cross-account → Account column
    assert "security_name" in cols and "action" in cols
    assert "payee_name" not in cols
    assert "running_balance" not in cols    # meaningless across accounts


def test_single_cash_account_keeps_cash_columns():
    m = TransactionTableModel(_repo(), account_id=5, invest=False)
    cols = _cols(m)
    assert "payee_name" in cols and "category_name" in cols
    assert "security_name" not in cols


def test_cross_account_cash_keeps_all_columns():
    m = TransactionTableModel(_repo(), account_id=None, invest=False)
    assert m.COLUMNS is m.COLUMNS_ALL
    assert "security_name" not in _cols(m)


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
