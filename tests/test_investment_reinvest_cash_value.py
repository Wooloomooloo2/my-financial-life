"""Investment dialog: a reinvested dividend shows its cash value (ADR-154).

A reinvest used to show Quantity and Price with no total, so there was no way to
check the numbers against the dividend figure on the statement. It now shares the
Buy/Sell tri-field group, with the total labelled **Dividend amount**:

- quantity + price fill the dividend amount (× the instrument multiplier);
- dividend amount + price fill the quantity (back out the shares);
- the commission leg is Buy/Sell-only, so it never inflates a reinvest's value;
- the stored cash impact stays 0 — a reinvest moves no cash (ADR-043).
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

from mfl_desktop.db.repository import Repository


def _repo_with_account():
    db = Path(tempfile.mkdtemp(prefix="mfl_reinv_")) / "m.mfl"
    repo = Repository(db)
    repo.create_account(
        name="MS 401(k)", type_key="investment", currency="USD",
        opening_balance=Decimal("0"),
    )
    return repo, repo.list_accounts()[0]


def _dialog(repo, acct, seed=None):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from mfl_desktop.ui.investment_transaction_dialog import (
        InvestmentTransactionDialog,
    )
    dlg = InvestmentTransactionDialog(repo, acct, seed=seed)
    if seed is None:
        dlg._action.setCurrentIndex(dlg._action.findData("ReinvDiv"))
    return dlg


def _label(dlg, field) -> str:
    return dlg._form.labelForField(field).text()


# ── the row is there, and named for what it is ───────────────────────────────


def test_reinvest_shows_dividend_amount_row():
    repo, acct = _repo_with_account()
    dlg = _dialog(repo, acct)
    assert dlg._total.isVisibleTo(dlg)
    assert _label(dlg, dlg._total) == "Dividend amount:"
    # It's a distribution, not a purchase — no commission leg.
    assert not dlg._commission.isVisibleTo(dlg)


def test_buy_still_says_total_cost():
    repo, acct = _repo_with_account()
    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Buy"))
    assert _label(dlg, dlg._total) == "Total cost:"
    assert dlg._commission.isVisibleTo(dlg)


# ── the maths solves both ways ───────────────────────────────────────────────


def test_quantity_and_price_fill_the_dividend_amount():
    repo, acct = _repo_with_account()
    dlg = _dialog(repo, acct)
    dlg._qty.setText("23.893205")
    dlg._price.setText("25.06")
    # The screenshot's row: 23.893205 × 25.06 = 598.76.
    assert Decimal(dlg._total.text()) == Decimal("598.76")
    assert "598.76" in dlg._hint.text()


def test_dividend_amount_and_price_back_out_the_shares():
    repo, acct = _repo_with_account()
    dlg = _dialog(repo, acct)
    dlg._price.setText("25.06")
    dlg._total.setText("598.76")
    # 598.76 ÷ 25.06 — the shares the statement's dividend bought.
    assert abs(Decimal(dlg._qty.text()) - Decimal("23.893057")) < Decimal("0.000001")


def test_commission_left_over_from_a_buy_does_not_inflate_the_value():
    repo, acct = _repo_with_account()
    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Buy"))
    dlg._qty.setText("10")
    dlg._price.setText("50")
    dlg._commission.setText("9.99")
    assert Decimal(dlg._total.text()) == Decimal("509.99")   # fee capitalised
    # Switching to a reinvest drops the fee leg: the value is exactly qty × price.
    dlg._action.setCurrentIndex(dlg._action.findData("ReinvDiv"))
    assert Decimal(dlg._total.text()) == Decimal("500.00")


def test_option_multiplier_still_applies():
    repo, acct = _repo_with_account()
    dlg = _dialog(repo, acct)
    dlg._instrument.setCurrentIndex(dlg._instrument.findData("option"))
    dlg._contract.setText("100")
    dlg._qty.setText("2")
    dlg._price.setText("1.50")
    assert Decimal(dlg._total.text()) == Decimal("300.00")   # 2 × 1.50 × 100


# ── it stays a zero-cash row ─────────────────────────────────────────────────


def test_saved_reinvest_still_moves_no_cash():
    repo, acct = _repo_with_account()
    dlg = _dialog(repo, acct)
    dlg._symbol.setText("PGINX")
    dlg._security.setEditText("PAX GLB ENVIRONMENTAL MKTS INS")
    dlg._qty.setText("23.893205")
    dlg._price.setText("25.06")
    assert Decimal(dlg._total.text()) == Decimal("598.76")
    dlg._on_save()

    rows = repo.list_transactions_for_account(acct.id)
    assert len(rows) == 1
    assert rows[0].action == "ReinvDiv"
    assert rows[0].amount == Decimal("0.00")                 # ADR-043
    assert abs(float(rows[0].quantity) - 23.893205) < 1e-9   # stored as REAL


def test_editing_a_reinvest_seeds_the_dividend_amount():
    repo, acct = _repo_with_account()
    dlg = _dialog(repo, acct)
    dlg._symbol.setText("PGINX")
    dlg._security.setEditText("PAX GLB ENVIRONMENTAL MKTS INS")
    dlg._qty.setText("23.893205")
    dlg._price.setText("25.06")
    dlg._on_save()

    seed = repo.list_transactions_for_account(acct.id)[0]
    edit = _dialog(repo, acct, seed=seed)
    # Re-opening the row shows the cash value straight away — the point of the fix.
    assert Decimal(edit._total.text()) == Decimal("598.76")
    assert _label(edit, edit._total) == "Dividend amount:"


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
