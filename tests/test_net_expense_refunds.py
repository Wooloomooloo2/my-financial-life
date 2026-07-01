"""Report aggregates net refunds/reimbursements on expense categories (ADR-129).

Before ADR-129 the flow reports used a **strict-outflow** definition
(``kind='expense' AND amount < 0``), so a reimbursement (a positive amount on
an expense category) was ignored and a bar showed the gross outflow — e.g. a
"Reimbursed" category showed £1,730.79 while the true net was ~£45. Now every
expense line contributes its **signed** amount, so refunds reduce the category;
a category that nets ≤ £0 is clamped (dropped) so a stacked bar stays positive.

This pins the shared behaviour across all five aggregates: spending, payee,
category×payee, sankey, and the income/expense series (whose expense bar must
equal the spending total for the same scope). Income stays strict-inflow.

Qt-free — runs on the base interpreter (``python3 tests/test_net_expense_refunds.py``)
or under pytest.
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.db.repository import Repository

_FROM, _TO, _BUCKET = "2026-01-01", "2026-01-31", "2026-01"


def _tx(repo, *, account_id, category_id, payee_id, amount):
    repo.insert_transaction(
        account_id=account_id, posted_date="2026-01-15",
        amount=Decimal(amount), payee_id=payee_id, category_id=category_id,
        status="Reconciled", memo="", import_hash=None, import_batch_id=None,
    )


def _build():
    """A repo with two expense categories: 'Meals' nets positive (spend £100,
    £30 refunded → £70) and 'Reimbursed' nets negative (spend £50, £80 back →
    -£30, must clamp). Distinct payees so the payee/cell tests are clean. Plus
    an income category with a small negative correction, to prove income stays
    strict-inflow."""
    db = Path(tempfile.mkdtemp(prefix="mfl_net_")) / "money.mfl"
    repo = Repository(db)
    acct = repo.create_account(name="Current", type_key="cash", currency="GBP").id
    meals = repo.create_category("Meals", None, "expense")
    reimb = repo.create_category("Reimbursed", None, "expense")
    salary = repo.create_category("Salary", None, "income")
    cafe = repo.create_payee("Cafe")
    workcorp = repo.create_payee("WorkCorp")
    employer = repo.create_payee("Employer")

    _tx(repo, account_id=acct, category_id=meals, payee_id=cafe, amount="-100.00")
    _tx(repo, account_id=acct, category_id=meals, payee_id=cafe, amount="30.00")     # refund
    _tx(repo, account_id=acct, category_id=reimb, payee_id=workcorp, amount="-50.00")
    _tx(repo, account_id=acct, category_id=reimb, payee_id=workcorp, amount="80.00")  # over-refund
    _tx(repo, account_id=acct, category_id=salary, payee_id=employer, amount="200.00")
    _tx(repo, account_id=acct, category_id=salary, payee_id=employer, amount="-10.00")  # income correction
    return repo, {"acct": acct, "meals": meals, "reimb": reimb, "salary": salary,
                  "cafe": cafe, "workcorp": workcorp, "employer": employer}


# ── spending_aggregates ──────────────────────────────────────────────────────


def test_spending_nets_refund_and_clamps_negative():
    repo, ids = _build()
    rows = repo.spending_aggregates(
        date_from=_FROM, date_to=_TO, granularity="month",
    )
    by_cat = {r["category_id"]: r["spending_pence"] for r in rows}
    assert by_cat.get(ids["meals"]) == 7000            # £100 − £30 = £70
    assert ids["reimb"] not in by_cat                  # −£30 nets ≤ 0 → dropped
    assert all(r["spending_pence"] > 0 for r in rows)  # bars stay positive


# ── payee_spending_aggregates ────────────────────────────────────────────────


def test_payee_nets_refund_and_clamps_negative():
    repo, ids = _build()
    res = repo.payee_spending_aggregates(date_from=_FROM, date_to=_TO)
    by_payee = {p["payee_id"]: p["spending_pence"] for p in res["payees"]}
    assert by_payee.get(ids["cafe"]) == 7000
    assert ids["workcorp"] not in by_payee             # payee nets −£30 → dropped


# ── category_payee_matrix ────────────────────────────────────────────────────


def test_matrix_nets_refund_and_clamps_negative():
    repo, ids = _build()
    res = repo.category_payee_matrix(date_from=_FROM, date_to=_TO)
    cells = {(c["category_id"], c["payee_id"]): c["spending_pence"]
             for c in res["cells"]}
    assert cells.get((ids["meals"], ids["cafe"])) == 7000
    assert (ids["reimb"], ids["workcorp"]) not in cells


# ── sankey_category_totals ───────────────────────────────────────────────────


def test_sankey_nets_refund_and_clamps_negative():
    repo, ids = _build()
    res = repo.sankey_category_totals(date_from=_FROM, date_to=_TO)
    assert res["expense"].get(ids["meals"]) == 7000
    assert ids["reimb"] not in res["expense"]          # clamped
    # Income stays strict-inflow: the −£10 correction is ignored, not netted.
    assert res["income"].get(ids["salary"]) == 20000


# ── income_expense_series ────────────────────────────────────────────────────


def test_income_expense_series_expense_equals_spending_total():
    repo, ids = _build()
    res = repo.income_expense_series(
        date_from=_FROM, date_to=_TO, granularity="month",
    )
    # Expense bucket = per-category floored net = £70 (Meals) + £0 (Reimbursed).
    # Equals the spending_aggregates total for the same scope → reports agree.
    assert res["expense"].get(_BUCKET) == 7000
    # Income strict: only the +£200, the −£10 correction ignored.
    assert res["income"].get(_BUCKET) == 20000


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
