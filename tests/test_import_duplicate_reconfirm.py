"""A skipped exact-hash duplicate still re-confirms the row it duplicates (ADR-186).

Re-downloading a period already imported hands back the same FITID, so the
incoming copy is skipped. Before ADR-186 the skip walked straight past the
existing row, which meant a transaction knocked back down the confidence ladder
(a bulk status edit, an accidental un-clear) could *never* be repaired — every
later download of that period is a duplicate and was ignored.

Now the skip re-confirms: ``pending``/``cleared`` climb to ``matched`` and a
missing ``bank_posted_date`` is filled, while ``reconciled`` stays locked and
nothing else on the row is touched.

Qt-free — ``python3 tests/test_import_duplicate_reconfirm.py`` or under pytest.
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
    ClassifiedTransaction, ImportService, PendingImport,
)


def _repo():
    db = Path(tempfile.mkdtemp(prefix="mfl_reconf_")) / "money.mfl"
    repo = Repository(db)
    acct = repo.create_account(name="Cur", type_key="cash", currency="GBP")
    cat = repo.create_category("Food", None, "expense")
    return repo, acct, cat


def _row(repo, tid):
    r = repo._conn.execute(
        "SELECT status, bank_posted_date, amount, memo, posted_date, category_id "
        "FROM txn WHERE id=?", (tid,)
    ).fetchone()
    return dict(r)


def _seed(repo, acct, cat, *, status, import_hash, bank_posted_date=None,
          posted_date="2026-07-06", memo="mine"):
    return repo.insert_transaction(
        account_id=acct.id, posted_date=posted_date, amount=Decimal("-8.25"),
        payee_id=None, category_id=cat, status=status, memo=memo,
        import_hash=import_hash, import_batch_id=None,
        bank_posted_date=bank_posted_date,
    )


# ── repository backbone ─────────────────────────────────────────────────────


def test_reconfirm_advances_pending_and_cleared_only():
    repo, acct, cat = _repo()
    for start, expect, advanced in (
        ("pending", "matched", True),
        ("cleared", "matched", True),
        ("matched", "matched", False),      # already at the rung
        ("reconciled", "reconciled", False),  # locked
    ):
        tid = _seed(repo, acct, cat, status=start, import_hash=f"H-{start}")
        got = repo.reconfirm_by_import_hash(acct.id, f"H-{start}", "2026-07-07")
        assert got is advanced, f"{start}: returned {got}, expected {advanced}"
        assert _row(repo, tid)["status"] == expect, f"{start} -> {expect}"


def test_reconfirm_fills_missing_bank_date_and_keeps_an_existing_one():
    repo, acct, cat = _repo()
    absent = _seed(repo, acct, cat, status="cleared", import_hash="H-absent")
    present = _seed(repo, acct, cat, status="cleared", import_hash="H-present",
                    bank_posted_date="2026-07-07")
    repo.reconfirm_by_import_hash(acct.id, "H-absent", "2026-07-08")
    repo.reconfirm_by_import_hash(acct.id, "H-present", "2026-07-09")
    assert _row(repo, absent)["bank_posted_date"] == "2026-07-08"   # filled
    assert _row(repo, present)["bank_posted_date"] == "2026-07-07"  # first wins


def test_reconfirm_touches_nothing_but_status_and_bank_date():
    repo, acct, cat = _repo()
    tid = _seed(repo, acct, cat, status="cleared", import_hash="H1")
    before = _row(repo, tid)
    repo.reconfirm_by_import_hash(acct.id, "H1", "2026-07-07")
    after = _row(repo, tid)
    for field in ("amount", "memo", "posted_date", "category_id"):
        assert after[field] == before[field], f"{field} changed"


def test_reconfirm_is_scoped_to_the_account():
    repo, acct, cat = _repo()
    other = repo.create_account(name="Other", type_key="cash", currency="GBP")
    tid = _seed(repo, acct, cat, status="cleared", import_hash="H1")
    assert repo.reconfirm_by_import_hash(other.id, "H1", "2026-07-07") is False
    assert _row(repo, tid)["status"] == "cleared"       # untouched


def test_reconfirm_unknown_hash_is_a_no_op():
    repo, acct, _cat = _repo()
    assert repo.reconfirm_by_import_hash(acct.id, "nope", "2026-07-07") is False


# ── commit_import wiring ────────────────────────────────────────────────────


def _stage(svc, acct, rows):
    token = "tok"
    svc._pending[token] = PendingImport(
        token=token, account_id=acct.id, account_iri=acct.iri,
        account_name=acct.name, filename="x.ofx", file_format="ofx",
        transactions=rows,
    )
    return token


def test_skipped_duplicate_repairs_a_downgraded_row():
    """The reported case: a row imported earlier, later knocked back to
    'cleared', is carried home by re-importing the overlapping period."""
    repo, acct, cat = _repo()
    downgraded = _seed(repo, acct, cat, status="cleared", import_hash="FIT-1")
    locked = _seed(repo, acct, cat, status="reconciled", import_hash="FIT-2")

    svc = ImportService(repo)
    token = _stage(svc, acct, [
        ClassifiedTransaction(
            fitid="FIT-1", date_iso="2026-07-07", amount=Decimal("8.25"),
            tx_type="debit", payee_raw="Pret", memo="", category_raw="",
            import_hash="FIT-1", status="duplicate",
        ),
        ClassifiedTransaction(
            fitid="FIT-2", date_iso="2026-07-07", amount=Decimal("8.25"),
            tx_type="debit", payee_raw="Pret", memo="", category_raw="",
            import_hash="FIT-2", status="duplicate",
        ),
    ])
    result = svc.commit_import(token, "cleared", accepted_match_fitids=set())

    assert result.imported == 0                 # no new rows — still a skip
    assert result.skipped == 2
    assert result.refreshed == 1                # only the downgraded one moved
    assert _row(repo, downgraded)["status"] == "matched"
    assert _row(repo, downgraded)["bank_posted_date"] == "2026-07-07"
    assert _row(repo, locked)["status"] == "reconciled"   # left alone


def test_reimporting_a_settled_period_reports_nothing_refreshed():
    """The ordinary re-import: everything already 'matched', so the run is a
    pure skip and the status bar has no re-confirmed clause to show."""
    repo, acct, cat = _repo()
    _seed(repo, acct, cat, status="matched", import_hash="FIT-1",
          bank_posted_date="2026-07-07")
    svc = ImportService(repo)
    token = _stage(svc, acct, [
        ClassifiedTransaction(
            fitid="FIT-1", date_iso="2026-07-07", amount=Decimal("8.25"),
            tx_type="debit", payee_raw="Pret", memo="", category_raw="",
            import_hash="FIT-1", status="duplicate",
        ),
    ])
    result = svc.commit_import(token, "cleared", accepted_match_fitids=set())
    assert (result.skipped, result.refreshed) == (1, 0)


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
