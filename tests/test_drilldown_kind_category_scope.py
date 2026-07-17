"""Income & Expense drill-down honours the report's category scope (ADR-169).

Regression guard for the bug where clicking an income / expense bar in a report
scoped to a *subset* of categories (e.g. "Bedford House ROI" → only Landlord
Expenses) opened a transaction list showing **every** category of that kind —
pet food, groceries, and the rest — even though the chart's bars only counted
the scoped categories.

The chart aggregate (``income_expense_series``) already applies
``t.category_id IN (...)``. The gap was purely in the drill-down: the kind
filter resolved its category set from ``list_categories_flat(kinds=...)`` — every
category of the kind — and never saw the report's ``category_ids``. The fix
threads the report's (descendant-expanded) scope through ``TxnListFilter`` into
``DrillDownFilterProxy.set_kind_filter``, which intersects it with the kind's
categories.

Needs PySide6 (the proxy is a ``QSortFilterProxyModel``); run offscreen under
the miniforge python3:

    QT_QPA_PLATFORM=offscreen \
    /opt/homebrew/Caskroom/miniforge/base/bin/python3 \
    tests/test_drilldown_kind_category_scope.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
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
)


def _setup():
    """One account with three expense categories: Landlord Expenses (in the
    report's scope) plus Groceries and Pet Food (out of scope). Each has one
    outflow so a scoped drill must show exactly one row."""
    db = Path(tempfile.mkdtemp(prefix="mfl_kind_scope_")) / "m.mfl"
    repo = Repository(db)
    acct = repo.create_account(name="Everyday", type_key="cash",
                               currency="GBP", opening_balance=Decimal("0"))
    landlord = repo.create_category("Landlord Expenses", None, "expense")
    groceries = repo.create_category("Groceries", None, "expense")
    petfood = repo.create_category("Pet Food", None, "expense")
    mk = lambda cat, amt: repo.insert_transaction(
        account_id=acct.id, posted_date="2026-04-15", amount=Decimal(amt),
        payee_id=None, category_id=cat, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    mk(landlord, "-500.00")   # in scope
    mk(groceries, "-80.00")   # out of scope
    mk(petfood, "-30.00")     # out of scope
    repo.commit()
    return repo, acct.id, landlord, groceries, petfood


def _proxy_for(repo, kind, kind_cat_ids):
    """Cross-account model + kind drill proxy, scoped as the window wires it:
    the kind's full category set intersected with the report's ``kind_cat_ids``
    (None == the pre-ADR-169 'all categories of the kind' behaviour)."""
    model = TransactionTableModel(repo, account_id=None)
    model.reload()
    all_of_kind = {c.id for c in repo.list_categories_flat(kinds=(kind,))}
    scoped = all_of_kind & set(kind_cat_ids) if kind_cat_ids is not None else all_of_kind
    proxy = DrillDownFilterProxy(model)
    proxy.set_kind_filter(kind, scoped)
    return model, proxy


def _category_ids_shown(model, proxy) -> set[int]:
    return {
        model.row_at(proxy.mapToSource(proxy.index(r, 0)).row()).category_id
        for r in range(proxy.rowCount())
    }


# ── proxy: the report's category scope narrows the kind drill ────────────────

def test_scope_excludes_out_of_scope_categories():
    repo, acct, landlord, groceries, petfood = _setup()
    model, proxy = _proxy_for(repo, "expense", [landlord])
    shown = _category_ids_shown(model, proxy)
    assert shown == {landlord}           # only the scoped category…
    assert groceries not in shown        # …not groceries…
    assert petfood not in shown          # …nor pet food
    assert proxy.rowCount() == 1


def test_no_scope_is_all_of_the_kind():
    repo, acct, landlord, groceries, petfood = _setup()
    # None scope → every expense category, the pre-ADR-169 behaviour (still
    # valid for a report that spans all categories).
    model, proxy = _proxy_for(repo, "expense", None)
    assert _category_ids_shown(model, proxy) == {landlord, groceries, petfood}
    assert proxy.rowCount() == 3


def test_empty_intersection_matches_nothing():
    """A scope with no category of the clicked kind must show nothing — not
    fall back to showing every category. Empty set != None in set_kind_filter."""
    repo, acct, landlord, groceries, petfood = _setup()
    proxy = DrillDownFilterProxy(TransactionTableModel(repo, account_id=None))
    proxy.sourceModel().reload()
    proxy.set_kind_filter("expense", set())   # nothing in scope
    assert proxy.rowCount() == 0


# ── split lines: the report counts them, so the drill must too ───────────────

def _split_setup():
    """The Bedford-House shape: an income category that appears only on a
    *split line* of a transaction whose parent is Uncategorised. The report
    aggregates `txn_category_line`, so its income bar counts the line — the
    drill (matching the parent's category) must surface the split parent, not
    an empty list."""
    db = Path(tempfile.mkdtemp(prefix="mfl_kind_split_")) / "m.mfl"
    repo = Repository(db)
    acct = repo.create_account(name="Rental", type_key="cash",
                               currency="GBP", opening_balance=Decimal("0"))
    rental = repo.create_category("Landlord - Rental Income", None, "income")
    fees = repo.create_category("Rental Fees", None, "expense")
    # A receipt split into a rental-income line and an expense line; the parent
    # keeps Uncategorised, exactly as the live file records it.
    txn_id = repo.insert_split_transaction(
        account_id=acct.id, posted_date="2023-06-15", payee_id=None,
        status="cleared", memo="", total_amount=Decimal("1150.00"),
        lines=[
            (rental, None, Decimal("1200.00")),
            (fees, None, Decimal("-50.00")),
        ],
        import_hash=None, import_batch_id=None,
    )
    repo.commit()
    return repo, acct.id, rental, fees, txn_id


def _drill_ids(repo, acct_id, kind, scope):
    from datetime import date as _date
    flt = TxnListFilter.for_kind(
        account_id=acct_id, account_name="Rental",
        kind=kind, kind_label=kind.title(),
        period_key="custom",
        custom_start=_date(2023, 1, 1), custom_end=_date(2023, 12, 31),
        kind_category_ids=tuple(scope),
    )
    win = TransactionsListWindow(repo, flt)
    try:
        proxy, model = win._proxy, win._model
        return [
            model.row_at(proxy.mapToSource(proxy.index(r, 0)).row()).id
            for r in range(proxy.rowCount())
        ]
    finally:
        win.close()


def test_income_on_a_split_line_is_surfaced():
    repo, acct, rental, fees, txn_id = _split_setup()
    # Report scoped to the rental income category — which lives only on a split
    # line of a parent that is Uncategorised.
    shown = _drill_ids(repo, acct, "income", (rental,))
    assert shown == [txn_id], "the split parent should surface via its line"


def test_split_expense_line_drills_under_expense_scope():
    repo, acct, rental, fees, txn_id = _split_setup()
    # The same transaction's expense line belongs to the expense bar; scoping to
    # the expense category must surface the same parent under the expense drill.
    shown = _drill_ids(repo, acct, "expense", (fees,))
    assert shown == [txn_id]


def test_split_parent_out_of_scope_line_is_excluded():
    repo, acct, rental, fees, txn_id = _split_setup()
    # A scope naming neither of the split's line categories must not surface the
    # parent — the split-aware match is a real intersection, not "any split".
    other = repo.create_category("Groceries", None, "expense")
    assert _drill_ids(repo, acct, "expense", (other,)) == []


# ── end-to-end: for_kind carries the scope into the window ───────────────────

def test_for_kind_scope_threads_through_window():
    repo, acct, landlord, groceries, petfood = _setup()
    flt = TxnListFilter.for_kind(
        account_id=acct, account_name="Everyday",
        kind="expense", kind_label="Expense",
        period_key="custom",
        custom_start=date(2026, 4, 1), custom_end=date(2026, 4, 30),
        kind_category_ids=(landlord,),
    )
    win = TransactionsListWindow(repo, flt)
    try:
        proxy, model = win._proxy, win._model
        shown = {
            model.row_at(proxy.mapToSource(proxy.index(r, 0)).row()).category_id
            for r in range(proxy.rowCount())
        }
        assert shown == {landlord}, "scoped drill should show only Landlord"
    finally:
        win.close()


def test_for_kind_without_scope_shows_all():
    repo, acct, landlord, groceries, petfood = _setup()
    flt = TxnListFilter.for_kind(
        account_id=acct, account_name="Everyday",
        kind="expense", kind_label="Expense",
        period_key="custom",
        custom_start=date(2026, 4, 1), custom_end=date(2026, 4, 30),
    )
    win = TransactionsListWindow(repo, flt)
    try:
        proxy, model = win._proxy, win._model
        shown = {
            model.row_at(proxy.mapToSource(proxy.index(r, 0)).row()).category_id
            for r in range(proxy.rowCount())
        }
        assert shown == {landlord, groceries, petfood}
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
