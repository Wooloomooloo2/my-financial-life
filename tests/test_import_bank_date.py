"""Import → matched flip + bank_posted_date — Phase 3a (ADR-130).

When a download matches a hand-entered transaction it should climb the ladder to
``matched`` and record the bank's posting date (separate from the user's spend
date), so reconciliation ranges on the bank date. New-from-bank rows stamp the
bank date too. This pins the repository backbone and the commit_import wiring.

Qt-free — ``python3 tests/test_import_bank_date.py`` or under pytest.
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import txn_status
from mfl_desktop.db.repository import Repository
from mfl_desktop.import_engine.import_service import (
    ClassifiedTransaction, ImportService, PendingImport,
)


def _repo():
    db = Path(tempfile.mkdtemp(prefix="mfl_bpd_")) / "money.mfl"
    repo = Repository(db)
    acct = repo.create_account(name="Cur", type_key="cash", currency="GBP")
    cat = repo.create_category("Food", None, "expense")
    return repo, acct, cat


def _status(repo, tid):
    r = repo._conn.execute(
        "SELECT status, bank_posted_date FROM txn WHERE id=?", (tid,)
    ).fetchone()
    return r[0], r[1]


# ── repository backbone ─────────────────────────────────────────────────────


def test_insert_stores_bank_posted_date():
    repo, acct, cat = _repo()
    tid = repo.insert_transaction(
        account_id=acct.id, posted_date="2026-06-22", amount=Decimal("-8.25"),
        payee_id=None, category_id=cat, status="pending", memo="",
        import_hash=None, import_batch_id=None, bank_posted_date="2026-06-23",
    )
    assert _status(repo, tid) == ("pending", "2026-06-23")


def test_merge_advances_status_and_records_bank_date():
    repo, acct, cat = _repo()
    mk = lambda s: repo.insert_transaction(
        account_id=acct.id, posted_date="2026-06-22", amount=Decimal("-8.25"),
        payee_id=None, category_id=cat, status=s, memo="",
        import_hash=None, import_batch_id=None,
    )
    for start, expect in (("pending", "matched"), ("cleared", "matched"),
                          ("matched", "matched"), ("reconciled", "reconciled")):
        tid = mk(start)
        repo.merge_into_manual_transaction(
            tid, import_hash=f"H-{start}", memo="", bank_posted_date="2026-06-23",
        )
        status, bpd = _status(repo, tid)
        assert status == expect, f"{start} -> {status}, expected {expect}"
        assert bpd == "2026-06-23"           # bank date recorded regardless


def test_reconcilable_reports_bank_date():
    repo, acct, cat = _repo()
    tid = repo.insert_transaction(
        account_id=acct.id, posted_date="2026-06-22", amount=Decimal("-8.25"),
        payee_id=None, category_id=cat, status="matched", memo="",
        import_hash=None, import_batch_id=None, bank_posted_date="2026-06-23",
    )
    rows = repo.list_reconcilable_txns(acct.id)
    row = next(r for r in rows if r.id == tid)
    assert row.posted_date == "2026-06-23"   # bank date, not the spend date


# ── commit_import wiring ────────────────────────────────────────────────────


def test_import_match_flips_to_matched_with_bank_date():
    repo, acct, cat = _repo()
    # A hand-entered pending row (the match target), dated on the spend day.
    target = repo.insert_transaction(
        account_id=acct.id, posted_date="2026-06-22", amount=Decimal("-8.25"),
        payee_id=None, category_id=cat, status="pending", memo="",
        import_hash=None, import_batch_id=None,
    )
    svc = ImportService(repo)
    token = "tok"
    svc._pending[token] = PendingImport(
        token=token, account_id=acct.id, account_iri=acct.iri,
        account_name=acct.name, filename="x.ofx", file_format="ofx",
        transactions=[
            # download line matching the hand-entered row (bank date = 23rd)
            ClassifiedTransaction(
                fitid="F1", date_iso="2026-06-23", amount=Decimal("8.25"),
                tx_type="debit", payee_raw="Pret", memo="", category_raw="",
                import_hash="h1", status="potential_match",
                match_txn_id=target, match_is_manual=True,
            ),
            # a brand-new-from-bank line
            ClassifiedTransaction(
                fitid="F2", date_iso="2026-06-24", amount=Decimal("4.40"),
                tx_type="debit", payee_raw="Cafe Nero", memo="", category_raw="",
                import_hash="h2", status="new",
            ),
        ],
    )
    result = svc.commit_import(token, "matched", accepted_match_fitids={"F1"})
    assert result.matched == 1 and result.imported == 1

    # the hand-entered target climbed to matched + carries the bank date
    assert _status(repo, target) == ("matched", "2026-06-23")
    # the new-from-bank row was inserted as matched with its bank date stamped
    new = repo._conn.execute(
        "SELECT status, bank_posted_date FROM txn WHERE import_hash='h2'"
    ).fetchone()
    assert tuple(new) == ("matched", "2026-06-24")


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
