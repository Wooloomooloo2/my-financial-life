"""Reconcile transfers that live inside split lines (ADR-139).

The owner imported a Coop mortgage and tried to reconcile transfers from Smile
Current, where each payment was a SPLIT — principal (the transfer) + interest
(an expense) on one row. The old matcher only saw whole-txn totals (the £700
payment), so the £460.26 principal never matched the £460.26 mortgage credit.

The engine now offers split *lines* as transfer candidates, and the link path
stamps the transfer on the ``txn_split`` row + the counterpart txn.

Qt-free — ``python3 tests/test_transfer_reconcile_splits.py`` or under pytest.
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.db.repository import Repository, LinkExisting


def _setup():
    db = Path(tempfile.mkdtemp(prefix="mfl_xfer_")) / "m.mfl"
    repo = Repository(db)
    smile = repo.create_account(
        name="Smile Current", type_key="cash", currency="GBP",
        opening_balance=Decimal("5000.00"),
    )
    coop = repo.create_account(
        name="Coop Mortgage", type_key="cash", currency="GBP",
        opening_balance=Decimal("-100000.00"),
    )
    principal_cat = repo.create_category("Mortgage Principal", None, "expense")
    interest_cat = repo.create_category("Mortgage Interest", None, "expense")
    misc_cat = repo.create_category("Misc", None, "income")

    # Smile: one £700 payment split into principal (£460.26) + interest (£239.74)
    # — the principal line is a PLAIN line (not yet a transfer).
    parent_id = repo.insert_split_transaction(
        account_id=smile.id, posted_date="2012-09-15", payee_id=None,
        status="cleared", memo="Mortgage payment", total_amount=Decimal("-700.00"),
        lines=[
            (principal_cat, "Principal", Decimal("-460.26")),
            (interest_cat, "Interest", Decimal("-239.74")),
        ],
        import_hash=None, import_batch_id=None,
    )
    # Coop: the £460.26 principal credit (imported, standalone).
    coop_txn = repo.insert_transaction(
        account_id=coop.id, posted_date="2012-09-15", amount=Decimal("460.26"),
        payee_id=None, category_id=misc_cat, status="cleared", memo="Payment",
        import_hash=None, import_batch_id=None,
    )
    repo.commit()
    return repo, smile, coop, parent_id, coop_txn


def test_split_line_becomes_a_transfer_candidate():
    repo, smile, coop, parent_id, coop_txn = _setup()
    pairs = repo.find_transfer_pairs(
        account_a_id=smile.id, account_b_id=coop.id,
    )
    assert len(pairs) == 1, [
        (p.source_amount, p.target_amount, p.source_split_id) for p in pairs
    ]
    p = pairs[0]
    # Source is the £460.26 principal split line (outflow); target the credit.
    assert p.source_split_id is not None
    assert p.source_amount == Decimal("-460.26")
    assert p.target_amount == Decimal("460.26")
    assert p.source_split_memo == "Principal"
    # The £239.74 interest line has no counterpart, so it doesn't pair.


def test_linking_a_split_line_transfer_writes_both_sides():
    repo, smile, coop, parent_id, coop_txn = _setup()
    pairs = repo.find_transfer_pairs(
        account_a_id=smile.id, account_b_id=coop.id,
    )
    p = pairs[0]
    tcat = repo.get_default_transfer_category_id()
    result = repo.bulk_match_or_create_transfers([
        LinkExisting(
            source_txn_id=p.source_txn_id, candidate_txn_id=p.target_txn_id,
            category_id=tcat, source_split_id=p.source_split_id,
            candidate_split_id=p.target_split_id,
        )
    ])
    assert result.linked == 1

    # The split line now carries a transfer_id …
    line = repo.connection.execute(
        "SELECT transfer_id FROM txn_split WHERE id = ?", (p.source_split_id,),
    ).fetchone()
    assert line["transfer_id"] is not None
    # … shared with the Coop counterpart txn.
    coop_row = repo.connection.execute(
        "SELECT transfer_id FROM txn WHERE id = ?", (coop_txn,),
    ).fetchone()
    assert coop_row["transfer_id"] == line["transfer_id"]
    # A transfer parent row exists for that iri.
    par = repo.connection.execute(
        "SELECT from_account_id, to_account_id FROM transfer WHERE iri = ?",
        (line["transfer_id"],),
    ).fetchone()
    assert (par["from_account_id"], par["to_account_id"]) == (smile.id, coop.id)

    # The split editor now shows the principal line as a transfer to Coop.
    lines = repo.split_lines_for_txns([parent_id])[parent_id]
    principal = next(l for l in lines if l.amount == Decimal("-460.26"))
    assert principal.transfer_to_account_id == coop.id

    # The parent stays a split (transfer_id NULL); balances are unchanged.
    parent = repo.connection.execute(
        "SELECT transfer_id, amount FROM txn WHERE id = ?", (parent_id,),
    ).fetchone()
    assert parent["transfer_id"] is None and parent["amount"] == -70000


def test_already_matched_split_line_not_re_offered():
    repo, smile, coop, parent_id, coop_txn = _setup()
    p = repo.find_transfer_pairs(account_a_id=smile.id, account_b_id=coop.id)[0]
    repo.bulk_match_or_create_transfers([
        LinkExisting(
            source_txn_id=p.source_txn_id, candidate_txn_id=p.target_txn_id,
            category_id=repo.get_default_transfer_category_id(),
            source_split_id=p.source_split_id,
        )
    ])
    # Second pass: the linked line + counterpart are gone from the pool.
    assert repo.find_transfer_pairs(
        account_a_id=smile.id, account_b_id=coop.id,
    ) == []


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
