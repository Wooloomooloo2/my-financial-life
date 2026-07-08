"""Investment dialog: remember the cash-dividend category + never default to
'matched' for a manual entry (ADR-142).

- A cash dividend remembers its last-used category (e.g. *Dividend Income*)
  instead of always seeding the generic *Investment income*, mirroring the
  existing reinvested-dividend behaviour (ADR-089).
- A manual investment entry defaults to *pending*, never *matched* (matched is
  the OFX-download state, ADR-130) — matching the register dialog.
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import txn_status
from mfl_desktop.db.repository import Repository


def _repo_with_account():
    db = Path(tempfile.mkdtemp(prefix="mfl_div_")) / "m.mfl"
    repo = Repository(db)
    repo.create_account(
        name="Brokerage", type_key="investment", currency="GBP",
        opening_balance=Decimal("0"),
    )
    acct = repo.list_accounts()[0]
    div_cat = repo.create_category("Dividend Income", None, "income")
    return repo, acct, div_cat


# ── repository setting ───────────────────────────────────────────────────────


def test_dividend_category_setting_round_trip():
    repo, acct, div_cat = _repo_with_account()
    assert repo.get_dividend_category_id() is None          # unset
    repo.set_dividend_category_id(div_cat)
    assert repo.get_dividend_category_id() == div_cat
    repo.set_dividend_category_id(None)
    assert repo.get_dividend_category_id() is None


def test_dividend_category_falls_back_when_category_re_kinded():
    repo, acct, div_cat = _repo_with_account()
    repo.set_dividend_category_id(div_cat)
    # If the stored category stops being an income category, don't mis-file:
    repo.connection.execute(
        "UPDATE category SET kind='expense' WHERE id=?", (div_cat,),
    )
    repo.commit()
    assert repo.get_dividend_category_id() is None


# ── dialog ───────────────────────────────────────────────────────────────────


def _dialog(repo, acct):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from mfl_desktop.ui.investment_transaction_dialog import (
        InvestmentTransactionDialog,
    )
    return InvestmentTransactionDialog(repo, acct)


def test_manual_entry_defaults_to_pending_not_matched():
    repo, acct, div_cat = _repo_with_account()
    dlg = _dialog(repo, acct)
    assert dlg._status.currentText() == txn_status.label(txn_status.PENDING)
    assert dlg._status.currentText() != txn_status.label(txn_status.MATCHED)


def test_cash_dividend_uses_remembered_category():
    from mfl_desktop.ui.category_picker import selected_category_id
    repo, acct, div_cat = _repo_with_account()
    inv_income = repo.find_or_create_category_path(["Income", "Investment income"])

    # Unset → a cash dividend seeds the generic Investment income.
    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Div"))
    assert selected_category_id(dlg._category) == inv_income

    # Remember Dividend Income → a fresh dialog's cash dividend seeds that.
    repo.set_dividend_category_id(div_cat)
    dlg2 = _dialog(repo, acct)
    dlg2._action.setCurrentIndex(dlg2._action.findData("Div"))
    assert selected_category_id(dlg2._category) == div_cat


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
