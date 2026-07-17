"""A split shows in a category list only when a *line* matches (ADR-176).

Owner-reported, with a screenshot: the Uncategorised transaction list was full
of "—Split—" rows whose split lines were all categorised. A split transaction
has no own category — its lines carry them (ADR-051), and the parent's stored
``category_id`` is a placeholder, in practice Uncategorised. Both filter
proxies matched that placeholder, so every split leaked into the Uncategorised
list whether or not any line was genuinely uncategorised.

The rule these lock down: for a split, a category filter consults the **lines
alone**. So —

- a split whose lines are all categorised does **not** appear under
  Uncategorised (the bug);
- a split with a genuinely uncategorised line **does**;
- filtering by a line's own category still surfaces the "—Split—" row
  (ADR-051 preserved);
- a plain (non-split) uncategorised transaction is unaffected.

Both the base proxy (``TransactionFilterProxy.set_category_id``, kept capable
for non-UI callers) and the drill proxy (``set_category_descendant_ids``, what
the Uncategorised list actually uses) are covered.

    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_drilldown_split_uncategorised.py
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

from mfl_desktop.db.repository import Repository, UNCATEGORISED_ID
from mfl_desktop.ui.filter_proxy import TransactionFilterProxy
from mfl_desktop.ui.register_model import TransactionTableModel
from mfl_desktop.ui.transactions_list_window import (
    TransactionsListWindow, TxnListFilter,
)

_D = Decimal


def _setup():
    """One account with three shapes of transaction, plus a couple of real
    categories to split into."""
    db = Path(tempfile.mkdtemp(prefix="mfl_splituncat_")) / "m.mfl"
    repo = Repository(db)
    acct = repo.create_account(name="Chase", type_key="cash", currency="GBP",
                               opening_balance=_D("0"))
    cash = repo.create_category("Cash", None, "expense")
    eating = repo.create_category("Eating Out", None, "expense")
    groc = repo.create_category("Groceries", None, "expense")

    # The screenshot's ATM split: parent Uncategorised, every line categorised.
    fully = repo.insert_split_transaction(
        account_id=acct.id, posted_date="2014-01-21", payee_id=None,
        status="reconciled", memo="", total_amount=_D("-80.00"),
        lines=[(cash, None, _D("-55.00")), (eating, None, _D("-25.00"))],
        import_hash=None, import_batch_id=None,
    )
    # A split with one genuinely uncategorised line.
    partial = repo.insert_split_transaction(
        account_id=acct.id, posted_date="2015-02-02", payee_id=None,
        status="cleared", memo="", total_amount=_D("-30.00"),
        lines=[(groc, None, _D("-20.00")),
               (UNCATEGORISED_ID, None, _D("-10.00"))],
        import_hash=None, import_batch_id=None,
    )
    # A plain, whole-transaction uncategorised row.
    plain = repo.insert_transaction(
        account_id=acct.id, posted_date="2012-10-11", amount=_D("-40.00"),
        payee_id=None, category_id=UNCATEGORISED_ID, status="cleared",
        memo="", import_hash=None, import_batch_id=None,
    )
    repo.commit()
    return repo, acct.id, {"fully": fully, "partial": partial, "plain": plain,
                           "cash": cash, "groc": groc}


# ── the drill proxy (what the Uncategorised list uses) ──────────────────────


def _drill_ids(repo, category_id) -> set[int]:
    flt = TxnListFilter.for_category(
        account_id=None, account_name="", category_id=category_id,
        category_label="", period_key="custom",
        custom_start=date(2010, 1, 1), custom_end=date(2025, 12, 31),
    )
    win = TransactionsListWindow(repo, flt)
    p, m = win._proxy, win._model
    ids = {
        m.row_at(p.mapToSource(p.index(r, 0)).row()).id
        for r in range(p.rowCount())
    }
    win.close()
    return ids


def test_fully_categorised_split_is_absent_from_uncategorised() -> None:
    repo, _acct, ids = _setup()
    shown = _drill_ids(repo, UNCATEGORISED_ID)
    assert ids["fully"] not in shown, (
        "a split whose lines are all categorised leaked into Uncategorised"
    )


def test_a_split_with_an_uncategorised_line_is_present() -> None:
    repo, _acct, ids = _setup()
    shown = _drill_ids(repo, UNCATEGORISED_ID)
    assert ids["partial"] in shown, (
        "a split with a genuinely uncategorised line must still show"
    )


def test_a_plain_uncategorised_row_is_present() -> None:
    repo, _acct, ids = _setup()
    assert ids["plain"] in _drill_ids(repo, UNCATEGORISED_ID)


def test_filtering_by_a_line_category_still_surfaces_the_split() -> None:
    """ADR-051 preserved: the split-line match is what makes a category that
    lives only on a split line drillable."""
    repo, _acct, ids = _setup()
    shown = _drill_ids(repo, ids["cash"])
    assert ids["fully"] in shown, "the split's own Cash line should surface it"


# ── the base proxy (kept capable for non-UI callers) ────────────────────────


def _base_ids(repo, acct_id, category_id) -> set[int]:
    model = TransactionTableModel(repo, acct_id)
    model.reload()
    proxy = TransactionFilterProxy(model)
    proxy.set_category_id(category_id)
    return {
        model.row_at(proxy.mapToSource(proxy.index(r, 0)).row()).id
        for r in range(proxy.rowCount())
    }


def test_base_proxy_matches_the_drill_proxy_on_uncategorised() -> None:
    repo, acct_id, ids = _setup()
    shown = _base_ids(repo, acct_id, UNCATEGORISED_ID)
    assert ids["fully"] not in shown
    assert ids["partial"] in shown
    assert ids["plain"] in shown


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print("\n" + ("all passed" if not failures else f"{failures} failed"))
    sys.exit(1 if failures else 0)
