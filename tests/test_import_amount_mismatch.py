"""Amount-mismatch matching + adopt-bank-amount — Phase 3b (ADR-130).

The £13.39 class of reconcile bug came from an £8.99 entered for an £8.25 bank
charge: the exact-amount matcher treated them as unrelated. Phase 3b adds a
conservative second pass — same sign, payee overlap, amount within a fraction —
that flags the pair as a **weak amount_differs** review item, and lets the user
"adopt the bank amount" on confirm.

Pure/Qt-free — ``python3 tests/test_import_amount_mismatch.py`` or under pytest.
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.import_engine import dedupe
from mfl_desktop.db.repository import Repository
from mfl_desktop.import_engine.import_service import (
    ClassifiedTransaction, ImportService, PendingImport,
)


def _imp(i, date, pence, payee):
    return dedupe.ImportRow(index=i, date_iso=date, amount_pence=pence, payee_raw=payee)


def _exist(id_, date, pence, payee, manual=True):
    return dedupe.ExistingRow(id=id_, date_iso=date, amount_pence=pence,
                              payee_name=payee, is_manual=manual)


# ── matcher: amount-mismatch tier ────────────────────────────────────────────


def test_near_amount_same_payee_flags_amount_differs():
    # bank £8.25 vs entered £8.99, both Pret, 1 day apart → weak amount_differs
    m = dedupe.match_duplicates(
        [_imp(0, "2026-06-23", -825, "PRET A MANGER")],
        [_exist(7, "2026-06-22", -899, "Pret A Manger")],
        fuzzy_amount_pct=0.20,
    )
    assert 0 in m
    assert m[0].existing_id == 7 and m[0].amount_differs
    assert m[0].existing_amount_pence == -899
    assert m[0].strength == "weak"


def test_no_payee_overlap_no_fuzzy_match():
    m = dedupe.match_duplicates(
        [_imp(0, "2026-06-23", -825, "PRET A MANGER")],
        [_exist(7, "2026-06-22", -899, "Greggs")],
        fuzzy_amount_pct=0.20,
    )
    assert m == {}                       # different payee → not the same charge


def test_amount_too_far_no_fuzzy_match():
    m = dedupe.match_duplicates(
        [_imp(0, "2026-06-23", -400, "PRET A MANGER")],   # £4 vs £8.99 = 55%
        [_exist(7, "2026-06-22", -899, "Pret A Manger")],
        fuzzy_amount_pct=0.20,
    )
    assert m == {}


def test_exact_still_beats_fuzzy_and_isnt_amount_differs():
    m = dedupe.match_duplicates(
        [_imp(0, "2026-06-23", -899, "Pret")],
        [_exist(7, "2026-06-22", -899, "Pret")],
        fuzzy_amount_pct=0.20,
    )
    assert 0 in m and not m[0].amount_differs


def test_fuzzy_off_by_default():
    m = dedupe.match_duplicates(
        [_imp(0, "2026-06-23", -825, "Pret")],
        [_exist(7, "2026-06-22", -899, "Pret")],
    )
    assert m == {}                       # no opt-in → exact-only (ADR-085 intact)


# ── commit_import: adopt bank amount ────────────────────────────────────────


def _repo_with_entry(amount):
    db = Path(tempfile.mkdtemp(prefix="mfl_amt_")) / "m.mfl"
    repo = Repository(db)
    acct = repo.create_account(name="Cur", type_key="cash", currency="GBP")
    cat = repo.create_category("Food", None, "expense")
    tid = repo.insert_transaction(
        account_id=acct.id, posted_date="2026-06-22", amount=Decimal(amount),
        payee_id=None, category_id=cat, status="pending", memo="",
        import_hash=None, import_batch_id=None,
    )
    return repo, acct, tid


def _amount(repo, tid):
    return repo._conn.execute(
        "SELECT amount FROM txn WHERE id=?", (tid,)
    ).fetchone()[0]


def _pending(repo, acct, tid):
    tx = ClassifiedTransaction(
        fitid="F1", date_iso="2026-06-23", amount=Decimal("8.25"),
        tx_type="debit", payee_raw="Pret", memo="", category_raw="",
        import_hash="h1", status="potential_match",
        match_txn_id=tid, match_is_manual=True,
        match_amount_differs=True, match_existing_amount=Decimal("-8.99"),
    )
    return PendingImport(
        token="t", account_id=acct.id, account_iri=acct.iri,
        account_name=acct.name, filename="x.ofx", file_format="ofx",
        transactions=[tx],
    )


def test_adopt_overwrites_amount():
    repo, acct, tid = _repo_with_entry("-8.99")
    svc = ImportService(repo); svc._pending["t"] = _pending(repo, acct, tid)
    svc.commit_import("t", "matched", accepted_match_fitids={"F1"},
                      adopt_amount_fitids={"F1"})
    assert _amount(repo, tid) == -825          # adopted the bank's £8.25


def test_confirm_without_adopt_keeps_amount():
    repo, acct, tid = _repo_with_entry("-8.99")
    svc = ImportService(repo); svc._pending["t"] = _pending(repo, acct, tid)
    svc.commit_import("t", "matched", accepted_match_fitids={"F1"})
    assert _amount(repo, tid) == -899          # kept the user's £8.99
    # ...but still climbed to matched
    assert repo._conn.execute(
        "SELECT status FROM txn WHERE id=?", (tid,)
    ).fetchone()[0] == "matched"


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
