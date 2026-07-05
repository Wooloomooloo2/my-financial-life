"""Income & Expense report: choose WHICH transfers to fold in (ADR-140).

The owner is building a rental-ROI view: rental income, operating expenses,
mortgage interest, and the mortgage *principal* — which is a transfer, not an
expense category. The report can now fold selected transfer categories in as
directional cash flows (outflow → expense, inflow → income), so a
'Mortgage Principal' transfer counts as an outflow while an unrelated
savings-transfer stays out.

Scope the report to the operating account so only that side of each transfer
counts (the counterpart leg lives on the other account, out of scope).

Qt-free repo/model tests + one offscreen dialog test.
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
from mfl_desktop.reports.filters import IncomeExpenseFilters


def _setup():
    db = Path(tempfile.mkdtemp(prefix="mfl_ie_")) / "m.mfl"
    repo = Repository(db)
    chk = repo.create_account(name="Rental Checking", type_key="cash",
                              currency="GBP", opening_balance=Decimal("0"))
    mort = repo.create_account(name="Rental Mortgage", type_key="cash",
                               currency="GBP", opening_balance=Decimal("-100000"))
    sav = repo.create_account(name="Savings", type_key="savings",
                              currency="GBP", opening_balance=Decimal("0"))
    rent = repo.create_category("Rent", None, "income")
    repairs = repo.create_category("Repairs", None, "expense")
    interest = repo.create_category("Mortgage Interest", None, "expense")
    princ = repo.create_category("Mortgage Principal", None, "transfer")
    gen = repo.get_default_transfer_category_id()
    mk = lambda a, c, amt: repo.insert_transaction(
        account_id=a, posted_date="2026-04-15", amount=Decimal(amt),
        payee_id=None, category_id=c, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    mk(chk.id, rent, "1000.00")
    mk(chk.id, repairs, "-200.00")
    mk(chk.id, interest, "-150.00")
    repo.create_transfer(from_account_id=chk.id, to_account_id=mort.id,
                         posted_date="2026-04-15", amount=Decimal("460.26"),
                         category_id=princ, status="cleared", memo="Principal")
    repo.create_transfer(from_account_id=chk.id, to_account_id=sav.id,
                         posted_date="2026-04-15", amount=Decimal("300.00"),
                         category_id=gen, status="cleared", memo="Save")
    repo.commit()
    return repo, chk, princ, gen


def _series(repo, chk, **kw):
    r = repo.income_expense_series(
        date_from="2026-04-01", date_to="2026-04-30", granularity="month",
        display_currency="GBP", account_ids=[chk.id], **kw,
    )
    return (sum(r["income"].values()) / 100, sum(r["expense"].values()) / 100)


def test_series_excludes_transfers_by_default():
    repo, chk, princ, gen = _setup()
    assert _series(repo, chk) == (1000.0, 350.0)


def test_series_includes_only_the_picked_transfer_category():
    repo, chk, princ, gen = _setup()
    # Mortgage principal (£460.26) folds into the expense side; the £300 savings
    # transfer (a different category) does not.
    assert _series(repo, chk, include_transfers=True,
                   transfer_category_ids=[princ]) == (1000.0, 810.26)


def test_series_all_transfers_when_none_picked():
    repo, chk, princ, gen = _setup()
    # include_transfers on + empty selection == every transfer category.
    assert _series(repo, chk, include_transfers=True) == (1000.0, 1110.26)


def test_composition_totals_include_the_transfer_category():
    repo, chk, princ, gen = _setup()
    totals = repo.sankey_category_totals(
        date_from="2026-04-01", date_to="2026-04-30", account_ids=[chk.id],
        display_currency="GBP", include_transfers=True,
        transfer_category_ids=[princ],
    )
    # The principal transfer shows on the expense side under its own category …
    assert totals["expense"].get(princ) == 46026
    # … and the savings transfer (not picked) is absent everywhere.
    assert gen not in totals["expense"] and gen not in totals["income"]


# ── filter model round-trip ─────────────────────────────────────────────────


def test_filters_json_round_trip_carries_transfer_categories():
    f = IncomeExpenseFilters(include_transfers=True,
                             transfer_category_ids=(7, 9))
    back = IncomeExpenseFilters.from_json(f.to_json())
    assert back.include_transfers is True
    assert back.transfer_category_ids == (7, 9)
    # Old blobs without the field default to empty.
    old = IncomeExpenseFilters.from_json('{"include_transfers":true}')
    assert old.transfer_category_ids == ()


# ── dialog ──────────────────────────────────────────────────────────────────


def test_dialog_transfer_panel_enables_with_checkbox():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from mfl_desktop.ui.income_expense_filter_dialog import IncomeExpenseFilterDialog
    repo, chk, princ, gen = _setup()
    accounts = repo.list_accounts()
    categories = repo.list_category_tree()
    dlg = IncomeExpenseFilterDialog(
        repo, current=IncomeExpenseFilters(), accounts=accounts,
        categories=categories,
    )
    # Off by default → transfer panel disabled.
    assert not dlg._transfer_categories_panel.isEnabled()
    dlg._include_transfers_check.setChecked(True)
    assert dlg._transfer_categories_panel.isEnabled()
    # The transfer panel lists the transfer category, not income/expense ones.
    ids = {cid for cid, _ in dlg._transfer_category_rows()}
    assert princ in ids
    dlg._transfer_categories_panel.set_checked_ids([princ])
    dlg._on_accept()
    assert dlg._result.include_transfers is True
    assert dlg._result.transfer_category_ids == (princ,)


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
