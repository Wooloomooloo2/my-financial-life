"""Drill-down honours a report's multi-account subset scope (ADR-147).

Regression guard for the bug where clicking a node in a Cash Flow (Sankey)
report scoped to a *set* of accounts (e.g. a rental-property group that
excludes a credit card) opened a transaction list showing **every** account's
rows — so an "Interest Exp" drill leaked the credit card's interest even
though the card wasn't in the report.

The report's aggregate (``sankey_category_totals``) already filters
``t.account_id IN (...)``. The gap was purely in the drill-down: the window
only carried a *single* ``account_id`` (or None = all), so a subset collapsed
to the cross-account view. The fix threads ``account_ids`` through
``TxnListFilter`` into ``DrillDownFilterProxy.set_account_ids``, which narrows
the cross-account model to exactly the report's accounts.

Needs PySide6 (the proxy is a ``QSortFilterProxyModel``); run offscreen under
the miniforge python3:

    QT_QPA_PLATFORM=offscreen \
    /opt/homebrew/Caskroom/miniforge/base/bin/python3 \
    tests/test_drilldown_account_subset.py
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

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.register_model import TransactionTableModel
from mfl_desktop.ui.transactions_list_window import (
    DrillDownFilterProxy, TransactionsListWindow, TxnListFilter,
    drilldown_account_scope,
)


def _setup():
    """Two rental accounts + a credit card, each with an Interest Exp txn."""
    db = Path(tempfile.mkdtemp(prefix="mfl_subset_")) / "m.mfl"
    repo = Repository(db)
    rent_chk = repo.create_account(name="Rental Checking", type_key="cash",
                                   currency="GBP", opening_balance=Decimal("0"))
    rent_mort = repo.create_account(name="Rental Mortgage", type_key="cash",
                                    currency="GBP",
                                    opening_balance=Decimal("-100000"))
    amex = repo.create_account(name="American Express", type_key="credit",
                               currency="GBP", opening_balance=Decimal("0"))
    interest = repo.create_category("Interest Exp", None, "expense")
    mk = lambda a, amt: repo.insert_transaction(
        account_id=a, posted_date="2026-04-15", amount=Decimal(amt),
        payee_id=None, category_id=interest, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    mk(rent_chk.id, "-120.00")   # rental interest, in scope
    mk(rent_mort.id, "-95.00")   # rental interest, in scope
    mk(amex.id, "-55.00")        # credit-card interest, OUT of scope
    repo.commit()
    return repo, rent_chk.id, rent_mort.id, amex.id, interest


def _proxy_for(repo, category_id, account_ids):
    """Cross-account model + drill proxy narrowed to a category subtree and
    (optionally) an account subset — mirrors how the window wires it."""
    model = TransactionTableModel(repo, account_id=None)
    model.reload()
    proxy = DrillDownFilterProxy(model)
    proxy.set_category_descendant_ids(repo.category_descendants(category_id))
    proxy.set_account_ids(set(account_ids) if account_ids else None)
    return model, proxy


def _account_ids_shown(model, proxy) -> set[int]:
    return {
        model.row_at(proxy.mapToSource(proxy.index(r, 0)).row()).account_id
        for r in range(proxy.rowCount())
    }


# ── proxy: the account subset narrows the drill-down ────────────────────────

def test_subset_excludes_out_of_scope_account():
    repo, chk, mort, amex, interest = _setup()
    model, proxy = _proxy_for(repo, interest, [chk, mort])
    shown = _account_ids_shown(model, proxy)
    assert shown == {chk, mort}          # both rentals…
    assert amex not in shown             # …but NOT the credit card
    assert proxy.rowCount() == 2


def test_no_subset_is_cross_account():
    repo, chk, mort, amex, interest = _setup()
    # No account_ids → the whole file, the pre-ADR-147 behaviour (still valid
    # when 0 accounts are selected in the report).
    model, proxy = _proxy_for(repo, interest, None)
    assert _account_ids_shown(model, proxy) == {chk, mort, amex}
    assert proxy.rowCount() == 3


def test_clearing_subset_widens_back_to_all():
    repo, chk, mort, amex, interest = _setup()
    model, proxy = _proxy_for(repo, interest, [chk, mort])
    assert proxy.rowCount() == 2
    proxy.set_account_ids(None)          # e.g. the chip's × removes the scope
    assert _account_ids_shown(model, proxy) == {chk, mort, amex}


# ── filter dataclass: account_ids threads through for_category ───────────────

def test_for_category_carries_account_ids():
    flt = TxnListFilter.for_category(
        account_id=None, account_name="",
        category_id=7, category_label="Interest Exp",
        period_key="custom",
        account_ids=(1, 2, 3), account_ids_label="3 accounts",
    )
    assert flt.account_ids == (1, 2, 3)
    assert flt.account_ids_label == "3 accounts"
    # The subset participates in the single-window-per-filter signature so a
    # rental scope opens its own window, distinct from cross-account.
    other = TxnListFilter.for_category(
        account_id=None, account_name="",
        category_id=7, category_label="Interest Exp", period_key="custom",
    )
    assert flt.signature() != other.signature()


def test_default_filter_has_no_subset():
    flt = TxnListFilter.for_category(
        account_id=4, account_name="Rental Checking",
        category_id=7, category_label="Interest Exp", period_key="custom",
    )
    assert flt.account_ids == ()          # single-account path unchanged


# ── scope helper: one / several / none accounts ─────────────────────────────

def test_scope_helper_single_account():
    aid, name, subset, label = drilldown_account_scope(
        [4], lambda i: {4: "Rental Checking"}[i],
    )
    assert (aid, name) == (4, "Rental Checking")
    assert subset == () and label == ""    # per-account drill, no subset

def test_scope_helper_subset():
    aid, name, subset, label = drilldown_account_scope([4, 5, 6], lambda i: "x")
    assert aid is None and name == ""      # no single account…
    assert subset == (4, 5, 6)             # …the subset carries the scope
    assert label == "3 accounts"

def test_scope_helper_none_is_cross_account():
    assert drilldown_account_scope([], lambda i: "x") == (None, "", (), "")


# ── editability: a split row's double-click opens the split dialog ──────────

def _split_setup():
    """A rental account with a *split* Interest Exp transaction — the shape the
    Cash Flow "Interest Exp" node drills into."""
    db = Path(tempfile.mkdtemp(prefix="mfl_splitdrill_")) / "m.mfl"
    repo = Repository(db)
    chk = repo.create_account(name="Rental Checking", type_key="cash",
                              currency="GBP", opening_balance=Decimal("0"))
    interest = repo.create_category("Interest Exp", None, "expense")
    repairs = repo.create_category("Repairs", None, "expense")
    txn_id = repo.insert_split_transaction(
        account_id=chk.id, posted_date="2026-04-15", payee_id=None,
        status="cleared", memo="", total_amount=Decimal("-120.00"),
        lines=[
            (interest, None, Decimal("-100.00")),
            (repairs, None, Decimal("-20.00")),
        ],
        import_hash=None, import_batch_id=None,
    )
    repo.commit()
    return repo, chk.id, interest, txn_id


def test_split_row_double_click_opens_split_dialog():
    repo, chk_id, interest, txn_id = _split_setup()
    flt = TxnListFilter.for_category(
        account_id=chk_id, account_name="Rental Checking",
        category_id=interest, category_label="Interest Exp",
        period_key="custom", custom_start=None, custom_end=None,
    )
    win = TransactionsListWindow(repo, flt)
    try:
        # The Interest Exp drill surfaces the split parent (via its line's
        # category), and the row is a split — non-editable inline.
        proxy, model = win._proxy, win._model
        split_rows = [
            r for r in range(proxy.rowCount())
            if model.row_at(proxy.mapToSource(proxy.index(r, 0)).row()).split_count
        ]
        assert split_rows, "the split Interest Exp parent should be listed"

        # Double-clicking it routes to the split detail dialog rather than
        # doing nothing — so the row is editable (ADR-147). Capture the seed
        # instead of opening a modal.
        seen = {}
        win._open_split_txn_dialog = lambda seed: seen.setdefault("id", seed.id)
        win._on_table_double_clicked(proxy.index(split_rows[0], 0))
        assert seen.get("id") == txn_id
    finally:
        win.close()


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
