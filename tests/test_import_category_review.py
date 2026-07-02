"""Import-time category control — transfer references, the new-category plan,
review decisions, and import-batch undo (ADR-118).

Regression guard for the gap where a generic-CSV import created categories the
user never had: a Banktivity transfer reference written in the category column
(bare ``[Chase Checking]`` or grouped ``Transfer:[Chase Checking]``) was made
into a bogus category, and genuinely-new names were created silently.

Qt-free: drives ``ImportService`` + ``Repository`` against a temp DB. Runs on
the base interpreter (``python3 tests/test_import_category_review.py``) or pytest.
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
from mfl_desktop.import_engine.import_service import (
    ImportService, _transfer_ref_account,
)


def _fresh_repo() -> Repository:
    repo = Repository(tempfile.mktemp(suffix=".mfl"))
    repo.connection.execute(
        "INSERT INTO person (iri, name, base_currency) "
        "VALUES ('mrl:Person_1', 'Me', 'GBP')"
    )
    repo.connection.execute(
        "INSERT INTO account (iri, name, type, family, currency) "
        "VALUES ('mrl:CashAccount_1', 'Amex', 'cash_std', 'cash', 'GBP')"
    )
    repo.commit()
    return repo


def _row(date, amount, payee, category, tx_type="debit", memo=""):
    return {
        "date": date, "amount": Decimal(amount), "tx_type": tx_type,
        "payee_raw": payee, "memo": memo, "category_raw": category,
    }


def _stage(svc, rows):
    return svc._classify_and_stage(
        rows, has_override=False, file_format="csv-generic",
        account_iri="mrl:CashAccount_1", filename="Amex Gold.csv",
    )


def test_transfer_ref_detection():
    assert _transfer_ref_account("[Chase Checking]") == "Chase Checking"
    assert _transfer_ref_account("Transfer:[Chase Checking]") == "Chase Checking"
    assert _transfer_ref_account("Household:General Items") is None
    assert _transfer_ref_account("Groceries") is None
    assert _transfer_ref_account("") is None
    assert _transfer_ref_account("[]") is None


def test_transfer_ref_not_made_a_category():
    repo = _fresh_repo()
    svc = ImportService(repo)
    before = repo.connection.execute("SELECT COUNT(*) c FROM category").fetchone()["c"]
    token = _stage(svc, [
        _row("2026-01-05", "500.00", "Payment", "Transfer:[Chase Checking]",
             tx_type="credit", memo="AUTOPAY"),
    ])
    # A transfer reference is never offered as a new category.
    assert svc.plan_new_categories(token) == []
    svc.commit_import(token, "matched", set())
    # No category was created; the row is Uncategorised with a transfer note.
    assert repo.connection.execute(
        "SELECT COUNT(*) c FROM category"
    ).fetchone()["c"] == before
    r = repo.connection.execute(
        "SELECT memo, category_id FROM txn"
    ).fetchone()
    assert r["category_id"] == repo.uncategorised_id()
    assert "Transfer from Chase Checking" in r["memo"]


def test_plan_lists_only_genuinely_new():
    repo = _fresh_repo()
    existing = repo.find_or_create_category_path(["Groceries"], source="user")
    svc = ImportService(repo)
    token = _stage(svc, [
        _row("2026-01-02", "10.00", "Tesco", "Groceries"),
        _row("2026-01-03", "20.00", "Shop", "Household:General Items"),
        _row("2026-01-04", "30.00", "Shop2", "Household:General Items"),
        _row("2026-01-05", "9.00", "X", ""),
        _row("2026-01-06", "5.00", "Pay", "[Chase Checking]", tx_type="credit"),
    ])
    plan = svc.plan_new_categories(token)
    # Existing path, empty, and the transfer ref are all excluded.
    assert [p.raw for p in plan] == ["Household:General Items"]
    assert plan[0].txn_count == 2
    assert existing  # silence unused


def test_map_decision_routes_and_persists():
    repo = _fresh_repo()
    target = repo.find_or_create_category_path(["Shopping"], source="user")
    svc = ImportService(repo)
    before = repo.connection.execute("SELECT COUNT(*) c FROM category").fetchone()["c"]
    token = _stage(svc, [
        _row("2026-01-03", "20.00", "Shop", "Household:General Items"),
    ])
    plan = svc.plan_new_categories(token)
    decisions = {plan[0].normalized: ("map", target)}
    svc.commit_import(token, "matched", set(), decisions)
    # Mapped to the chosen category, nothing new created.
    assert repo.connection.execute(
        "SELECT COUNT(*) c FROM category"
    ).fetchone()["c"] == before
    assert repo.connection.execute(
        "SELECT category_id FROM txn"
    ).fetchone()["category_id"] == target
    # And the mapping is durable for the next import.
    assert repo.get_category_import_mapping(["Household:General Items"]) == target


def test_review_decision_parks_in_needs_review():
    repo = _fresh_repo()
    svc = ImportService(repo)
    token = _stage(svc, [
        _row("2026-01-03", "20.00", "Shop", "Mystery Spend"),
    ])
    plan = svc.plan_new_categories(token)
    svc.commit_import(token, "matched", set(), {plan[0].normalized: ("review", None)})
    assert repo.connection.execute(
        "SELECT category_id FROM txn"
    ).fetchone()["category_id"] == repo.needs_review_category_id()


def test_undo_import_batch_removes_only_its_rows():
    repo = _fresh_repo()
    svc = ImportService(repo)
    t1 = _stage(svc, [_row("2026-01-02", "10.00", "A", "Groceries")])
    r1 = svc.commit_import(t1, "matched", set(),
                           {k.normalized: ("create", None)
                            for k in svc.plan_new_categories(t1)} or None)
    t2 = _stage(svc, [_row("2026-02-02", "20.00", "B", "Groceries")])
    r2 = svc.commit_import(t2, "matched", set())
    assert repo.connection.execute("SELECT COUNT(*) c FROM txn").fetchone()["c"] == 2
    result = repo.delete_import_batch(r2.batch_id)
    assert result["deleted_txns"] == 1
    assert repo.connection.execute("SELECT COUNT(*) c FROM txn").fetchone()["c"] == 1
    assert repo.connection.execute(
        "SELECT COUNT(*) c FROM import_batch WHERE id=?", (r2.batch_id,)
    ).fetchone()["c"] == 0
    # The first import's row is untouched.
    assert repo.connection.execute(
        "SELECT COUNT(*) c FROM txn WHERE import_batch_id=?", (r1.batch_id,)
    ).fetchone()["c"] == 1


def test_undo_reports_and_deletes_now_empty_import_categories():
    repo = _fresh_repo()
    svc = ImportService(repo)
    # Import creates a brand-new category "Mystery" (the user clicks Create).
    token = _stage(svc, [_row("2026-01-02", "10.00", "A", "Mystery")])
    r = svc.commit_import(
        token, "matched", set(),
        {k.normalized: ("create", None) for k in svc.plan_new_categories(token)},
    )
    mystery = repo.find_category_path(["Mystery"])
    assert mystery is not None and \
        repo.connection.execute(
            "SELECT source FROM category WHERE id=?", (mystery,)
        ).fetchone()["source"] == "import"

    result = repo.delete_import_batch(r.batch_id)
    empty_ids = [cid for cid, _path in result["empty_categories"]]
    assert mystery in empty_ids, result["empty_categories"]

    removed = repo.delete_empty_import_categories(empty_ids)
    assert removed == 1
    assert repo.find_category_path(["Mystery"]) is None
    # And no Needs-Review mapping was recorded (so a re-import re-offers it).
    assert repo.get_category_import_mapping(["Mystery"]) is None


def test_undo_keeps_category_still_used_by_another_batch():
    repo = _fresh_repo()
    svc = ImportService(repo)
    t1 = _stage(svc, [_row("2026-01-02", "10.00", "A", "Shared")])
    svc.commit_import(
        t1, "matched", set(),
        {k.normalized: ("create", None) for k in svc.plan_new_categories(t1)},
    )
    shared = repo.find_category_path(["Shared"])
    # A second import lands more rows on the same (now-existing) category.
    t2 = _stage(svc, [_row("2026-02-02", "20.00", "B", "Shared")])
    r2 = svc.commit_import(t2, "matched", set())
    # Undoing the second batch must NOT offer to delete the still-used category.
    result = repo.delete_import_batch(r2.batch_id)
    assert shared not in [cid for cid, _ in result["empty_categories"]]
    assert repo.find_category_path(["Shared"]) == shared


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
