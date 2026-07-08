"""Sankey (Cash Flow) report: fold transfers in, and pick which (ADR-146).

Mirrors the Income & Expense transfer option (ADR-140) on the Sankey report,
which shares ``sankey_category_totals``. With "Include transfers" on, a
``kind='transfer'`` leg folds into the diagram as a directional cash flow —
an outflow on the expense side, an inflow on the income side — and a picker
narrows which transfer categories fold in (empty == all).

A transfer has two legs (out of one account, into another); scoping the report
to one account leaves just that side's leg, which is the owner's ask ("show
transfers if only one account in the transfer is selected").

Qt-free repo/model tests + offscreen dialog/window tests.
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
from mfl_desktop.reports.filters import SankeyFilters


def _setup():
    db = Path(tempfile.mkdtemp(prefix="mfl_sankey_")) / "m.mfl"
    repo = Repository(db)
    chk = repo.create_account(name="Rental Checking", type_key="cash",
                              currency="GBP", opening_balance=Decimal("0"))
    mort = repo.create_account(name="Rental Mortgage", type_key="cash",
                               currency="GBP", opening_balance=Decimal("-100000"))
    sav = repo.create_account(name="Savings", type_key="savings",
                              currency="GBP", opening_balance=Decimal("0"))
    rent = repo.create_category("Rent", None, "income")
    repairs = repo.create_category("Repairs", None, "expense")
    princ = repo.create_category("Mortgage Principal", None, "transfer")
    gen = repo.get_default_transfer_category_id()
    mk = lambda a, c, amt: repo.insert_transaction(
        account_id=a, posted_date="2026-04-15", amount=Decimal(amt),
        payee_id=None, category_id=c, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    mk(chk.id, rent, "1000.00")
    mk(chk.id, repairs, "-200.00")
    repo.create_transfer(from_account_id=chk.id, to_account_id=mort.id,
                         posted_date="2026-04-15", amount=Decimal("460.26"),
                         category_id=princ, status="cleared", memo="Principal")
    repo.create_transfer(from_account_id=chk.id, to_account_id=sav.id,
                         posted_date="2026-04-15", amount=Decimal("300.00"),
                         category_id=gen, status="cleared", memo="Save")
    repo.commit()
    return repo, chk, mort, princ, gen


def _totals(repo, account_ids, **kw):
    return repo.sankey_category_totals(
        date_from="2026-04-01", date_to="2026-04-30",
        account_ids=account_ids, display_currency="GBP", **kw,
    )


# ── repo: transfer folding + single-account scoping ─────────────────────────

def test_default_excludes_transfers():
    repo, chk, mort, princ, gen = _setup()
    t = _totals(repo, [chk.id])
    assert princ not in t["expense"] and gen not in t["expense"]
    assert t["income"].get(next(iter(t["income"]))) == 100000  # rent £1000


def test_single_account_scope_shows_just_that_leg():
    repo, chk, mort, princ, gen = _setup()
    # Scoped to the checking account, the principal transfer's OUTflow leg
    # counts on the expense side; its inflow counterpart lives on the mortgage
    # account, out of scope, so it doesn't show as phantom income.
    t = _totals(repo, [chk.id], include_transfers=True,
                transfer_category_ids=[princ])
    assert t["expense"].get(princ) == 46026
    assert princ not in t["income"]


def test_both_accounts_scope_shows_both_legs():
    repo, chk, mort, princ, gen = _setup()
    # With both accounts in scope, both legs count — outflow on expense, the
    # matching inflow on income (same transfer category).
    t = _totals(repo, [chk.id, mort.id], include_transfers=True,
                transfer_category_ids=[princ])
    assert t["expense"].get(princ) == 46026
    assert t["income"].get(princ) == 46026


def test_picker_narrows_which_transfers():
    repo, chk, mort, princ, gen = _setup()
    # Only the picked category folds in; the £300 savings transfer stays out.
    t = _totals(repo, [chk.id], include_transfers=True,
                transfer_category_ids=[princ])
    assert t["expense"].get(princ) == 46026 and gen not in t["expense"]
    # Empty selection == all transfer categories.
    t_all = _totals(repo, [chk.id], include_transfers=True)
    assert t_all["expense"].get(princ) == 46026
    assert t_all["expense"].get(gen) == 30000


# ── filter model round-trip ─────────────────────────────────────────────────

def test_filters_json_round_trip_carries_transfers():
    f = SankeyFilters(include_transfers=True, transfer_category_ids=(7, 9))
    back = SankeyFilters.from_json(f.to_json())
    assert back.include_transfers is True
    assert back.transfer_category_ids == (7, 9)
    # Old blobs (no transfer fields) default off — the Sankey report is
    # unchanged for existing saved reports.
    old = SankeyFilters.from_json('{"period_key":"ytd","depth":2}')
    assert old.include_transfers is False and old.transfer_category_ids == ()


# ── dialog ──────────────────────────────────────────────────────────────────

def test_dialog_transfer_panel_enables_and_returns_selection():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from mfl_desktop.ui.sankey_filter_dialog import SankeyFilterDialog
    repo, chk, mort, princ, gen = _setup()
    dlg = SankeyFilterDialog(
        repo, accounts=repo.list_accounts(include_closed=True),
        categories=repo.list_category_tree(),
        current_account_ids=(), current_category_ids=(),
    )
    assert not dlg._transfer_categories_panel.isEnabled()   # off by default
    dlg._include_transfers_check.setChecked(True)
    assert dlg._transfer_categories_panel.isEnabled()
    # The transfer picker lists transfer categories, not income/expense.
    ids = {cid for cid, _ in dlg._transfer_category_rows()}
    assert princ in ids and gen in ids
    dlg._transfer_categories_panel.set_checked_ids([princ])
    dlg._on_accept()
    accounts, categories, include_transfers, tcats = dlg.values()
    assert include_transfers is True and tcats == (princ,)


# ── window: transfer categories render as nodes ─────────────────────────────

def test_window_renders_transfer_node_on_expense_side():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from mfl_desktop.ui.sankey_report_window import SankeyReportWindow
    repo, chk, mort, princ, gen = _setup()
    win = SankeyReportWindow.open_bare(repo)
    win._current_filters = win._with(
        period_key="custom", custom_start="2026-04-01", custom_end="2026-04-30",
        account_ids=(chk.id,), include_transfers=True,
        transfer_category_ids=(princ,),
    )
    captured: dict = {}
    win._chart.render = lambda **kw: captured.update(kw)   # type: ignore[assignment]
    win._refresh()
    expense = captured.get("expense", [])
    node = next((n for n in expense if n.label == "Mortgage Principal"), None)
    assert node is not None, [n.label for n in expense]
    assert abs(node.value - 460.26) < 0.001

    # Toggle transfers off → the transfer node disappears (diagram unchanged).
    win._current_filters = win._with(include_transfers=False,
                                     transfer_category_ids=())
    captured.clear()
    win._chart.render = lambda **kw: captured.update(kw)   # type: ignore[assignment]
    win._refresh()
    assert "Mortgage Principal" not in [
        n.label for n in captured.get("expense", [])
    ]


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
