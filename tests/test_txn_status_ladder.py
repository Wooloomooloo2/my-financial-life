"""Transaction status confidence ladder — Phase 1 foundation (ADR-130).

Pins the lowercase status ladder (pending → cleared → matched → reconciled):

- ``txn_status`` helpers (labels, key↔label, validity, locked);
- the DB round-trips the new lowercase keys and the migration-0033 CHECK
  rejects the old Title-case values;
- a grep-guard so the old ``'Pending'/'Uncleared'/'Cleared'/'Reconciled'``
  literals don't creep back into the code (they lived, copy-pasted, in ~8
  modules before centralisation).

Qt-free — ``python3 tests/test_txn_status_ladder.py`` or under pytest.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import txn_status
from mfl_desktop.db.repository import Repository


# ── helpers ──────────────────────────────────────────────────────────────────


def test_ladder_order_and_keys():
    assert txn_status.STATUSES == ("pending", "cleared", "matched", "reconciled")
    assert txn_status.labels() == ["Pending", "Cleared", "Matched", "Reconciled"]


def test_label_and_reverse_roundtrip():
    for key in txn_status.STATUSES:
        assert txn_status.key_for_label(txn_status.label(key)) == key
    assert txn_status.key_for_label("Matched") == "matched"
    assert txn_status.key_for_label("matched") == "matched"   # key also accepted
    assert txn_status.key_for_label("nope") is None


def test_validity_and_locked():
    assert all(txn_status.is_valid(s) for s in txn_status.STATUSES)
    assert not txn_status.is_valid("Cleared")   # old Title-case is no longer valid
    assert txn_status.is_locked("reconciled")
    assert not txn_status.is_locked("matched")


# ── DB round-trip + migration CHECK ─────────────────────────────────────────


def _repo():
    db = Path(tempfile.mkdtemp(prefix="mfl_status_")) / "money.mfl"
    repo = Repository(db)
    acct = repo.create_account(name="A", type_key="cash", currency="GBP").id
    cat = repo.create_category("Food", None, "expense")
    return repo, acct, cat


def test_each_status_round_trips_lowercase():
    repo, acct, cat = _repo()
    for i, status in enumerate(txn_status.STATUSES):
        tid = repo.insert_transaction(
            account_id=acct, posted_date="2026-01-0%d" % (i + 1),
            amount=Decimal("-1.00"), payee_id=None, category_id=cat,
            status=status, memo="", import_hash=None, import_batch_id=None,
        )
        row = repo._conn.execute(
            "SELECT status FROM txn WHERE id=?", (tid,)
        ).fetchone()
        assert row[0] == status


def test_check_rejects_old_titlecase_status():
    repo, acct, cat = _repo()
    try:
        repo._conn.execute(
            "INSERT INTO txn (iri, account_id, posted_date, amount, "
            " category_id, status) VALUES ('x:1', ?, '2026-01-01', -100, ?, ?)",
            (acct, cat, "Cleared"),
        )
    except sqlite3.IntegrityError:
        return  # CHECK correctly rejected the old value
    raise AssertionError("migration-0033 CHECK accepted the old 'Cleared' value")


# ── grep-guard against regressions ──────────────────────────────────────────


def test_no_old_status_literals_in_code():
    old = ("'Pending'", '"Pending"', "'Uncleared'", '"Uncleared"',
           "'Cleared'", '"Cleared"', "'Reconciled'", '"Reconciled"')
    offenders = []
    for py in (_REPO_ROOT / "mfl_desktop").rglob("*.py"):
        if py.name == "txn_status.py":     # the labels legitimately live here
            continue
        text = py.read_text(encoding="utf-8")
        if any(tok in text for tok in old):
            offenders.append(str(py.relative_to(_REPO_ROOT)))
    assert not offenders, f"old status literals remain in: {offenders}"


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
