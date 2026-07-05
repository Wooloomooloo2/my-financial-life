"""Budget funding model + no credit-limit-as-funds (ADR-138).

- `funding_mode` = 'balances' (default) or 'income' round-trips.
- 'available_credit' is gone: `set_budget_accounts` rejects it, and a credit
  card in the pool contributes its **signed balance** (its debt reduces the
  pool) rather than its limit.
- 'income' mode: the pool = income into the perimeter over the budget period,
  ignoring starting balances.

Qt-free — ``python3 tests/test_budget_funding_mode.py`` or under pytest.
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


def _repo():
    db = Path(tempfile.mkdtemp(prefix="mfl_bud_")) / "m.mfl"
    repo = Repository(db)
    cash = repo.create_account(
        name="Current", type_key="cash", currency="GBP",
        opening_balance=Decimal("2000.00"),
    )
    card = repo.create_account(
        name="Card", type_key="credit", currency="GBP",
        opening_balance=Decimal("0.00"), credit_limit=Decimal("5000.00"),
    )
    # card owes £318 (a spend); an income deposit of £1,500 into the current a/c
    salary_cat = repo.create_category("Salary", None, "income")
    food_cat = repo.create_category("Food", None, "expense")
    repo.insert_transaction(
        account_id=card.id, posted_date="2026-03-10", amount=Decimal("-318.00"),
        payee_id=None, category_id=food_cat, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    repo.insert_transaction(
        account_id=cash.id, posted_date="2026-03-25", amount=Decimal("1500.00"),
        payee_id=None, category_id=salary_cat, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    return repo, cash, card


def _pool(repo, bid):
    p, _excl = repo.compute_perimeter_pool(
        bid, display_ccy="GBP", on_date="2026-03-31",
    )
    return p


# ── funding_mode round-trip ─────────────────────────────────────────────────


def test_funding_mode_defaults_and_persists():
    repo, cash, card = _repo()
    b = repo.create_budget(name="B", start_month="2026-01", length_months=12)
    assert b.funding_mode == "balances"
    repo.set_budget_funding_mode(b.id, "income")
    assert repo.get_budget(b.id).funding_mode == "income"
    b2 = repo.create_budget(
        name="B2", start_month="2026-01", length_months=12, funding_mode="income",
    )
    assert b2.funding_mode == "income"


def test_available_credit_rejected():
    repo, cash, card = _repo()
    b = repo.create_budget(name="B", start_month="2026-01", length_months=12)
    try:
        repo.set_budget_accounts(b.id, [(card.id, "available_credit")])
    except ValueError:
        return
    raise AssertionError("set_budget_accounts accepted the dropped 'available_credit'")


# ── balances mode: credit card debt reduces the pool (no limit) ─────────────


def test_balances_pool_card_debt_reduces_not_limit():
    repo, cash, card = _repo()
    b = repo.create_budget(name="B", start_month="2026-01", length_months=12)
    # cash balance 2000 + 1500 salary = 3500; card balance = -318 (debt)
    repo.set_budget_accounts(b.id, [(cash.id, "balance"), (card.id, "balance")])
    # pool = 3500 (cash) + (-318) (card debt) = 3182 — NOT 3500 + 4682 headroom
    assert _pool(repo, b.id) == Decimal("3182.00")


def test_excluded_card_leaves_pool_at_cash_only():
    repo, cash, card = _repo()
    b = repo.create_budget(name="B", start_month="2026-01", length_months=12)
    repo.set_budget_accounts(b.id, [(cash.id, "balance"), (card.id, "excluded")])
    assert _pool(repo, b.id) == Decimal("3500.00")   # cash only; card ignored


# ── income mode: only income over the period, not balances ──────────────────


def test_income_pool_counts_income_over_period_not_balances():
    repo, cash, card = _repo()
    b = repo.create_budget(
        name="B", start_month="2026-01", length_months=12, funding_mode="income",
    )
    repo.set_budget_accounts(b.id, [(cash.id, "balance"), (card.id, "balance")])
    # only the £1,500 salary in-period counts; the £2,000 opening balance and
    # the card's debt do not.
    assert _pool(repo, b.id) == Decimal("1500.00")


def test_income_pool_ignores_income_outside_period():
    repo, cash, card = _repo()
    b = repo.create_budget(
        name="B", start_month="2026-05", length_months=2, funding_mode="income",
    )  # May–Jun 2026; the March salary is out of range
    repo.set_budget_accounts(b.id, [(cash.id, "balance")])
    assert _pool(repo, b.id) == Decimal("0.00")


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
