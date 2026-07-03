"""Reconcile-by-confidence — Phase 2 candidate gating (ADR-130).

The June reconcile mess happened because *any* non-reconciled row (including
``pending`` not-yet-at-the-bank purchases and duplicates) was tickable onto a
statement. Phase 2 gates the candidate set by the confidence ladder:

- ``matched`` (download-confirmed) is always eligible;
- ``cleared`` (seen by eye, not downloaded) is eligible only with
  ``include_cleared`` — for banks that offer no download;
- ``pending`` is **never** eligible;
- rows already ticked into the statement being resumed/viewed are always
  included so their ticks survive.

Qt-free — ``python3 tests/test_reconcile_confidence.py`` or under pytest.
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


def _build():
    db = Path(tempfile.mkdtemp(prefix="mfl_recon_")) / "money.mfl"
    repo = Repository(db)
    acct = repo.create_account(name="Current", type_key="cash", currency="GBP").id
    cat = repo.create_category("Food", None, "expense")
    ids = {}
    for i, status in enumerate(txn_status.STATUSES):
        ids[status] = repo.insert_transaction(
            account_id=acct, posted_date="2026-06-1%d" % i,
            amount=Decimal("-10.00"), payee_id=None, category_id=cat,
            status=status, memo="", import_hash=None, import_batch_id=None,
        )
    return repo, acct, ids


def _cand(repo, acct, **kw):
    return {t.id for t in repo.list_reconcilable_txns(acct, **kw)}


# ── candidate gating ─────────────────────────────────────────────────────────


def test_default_only_matched_eligible():
    repo, acct, ids = _build()
    cand = _cand(repo, acct)
    assert cand == {ids["matched"]}, cand
    # the June-mess guard: pending is never a candidate
    assert ids["pending"] not in cand
    assert ids["cleared"] not in cand


def test_include_cleared_adds_cleared_not_pending():
    repo, acct, ids = _build()
    cand = _cand(repo, acct, include_cleared=True)
    assert cand == {ids["matched"], ids["cleared"]}, cand
    assert ids["pending"] not in cand          # pending still never eligible


def test_resumed_statement_ticks_always_included():
    """A row ticked into the statement being resumed shows regardless of status
    (else its tick would be lost) — even a pending one."""
    repo, acct, ids = _build()
    # minimal open statement + a tick on the pending row
    sid = repo._conn.execute(
        "INSERT INTO statement (iri, account_id, start_date, end_date, "
        " starting_balance_pence, ending_balance_pence, status) "
        "VALUES ('s:1', ?, '2026-06-01', '2026-06-30', 0, 0, 'open')",
        (acct,),
    ).lastrowid
    repo._conn.execute(
        "INSERT INTO statement_txn (statement_id, txn_id) VALUES (?, ?)",
        (sid, ids["pending"]),
    )
    cand = _cand(repo, acct, include_statement_id=sid)
    assert ids["pending"] in cand              # resumed tick preserved
    assert ids["matched"] in cand              # still eligible on its own


# ── cleared-in-period count (the warning) ───────────────────────────────────


def test_count_cleared_in_period():
    repo, acct, ids = _build()
    # the cleared row is dated 2026-06-11 (index 1 in STATUSES)
    assert repo.count_cleared_in_period(acct, "2026-06-01", "2026-06-30") == 1
    assert repo.count_cleared_in_period(acct, "2026-07-01", "2026-07-31") == 0


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
