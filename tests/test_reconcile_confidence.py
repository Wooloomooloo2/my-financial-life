"""Reconcile candidates — visibility is not selection (ADR-189).

ADR-130 gated the *candidate set* by the confidence ladder: only ``matched``
rows were listed, with ``cleared`` and ``pending`` behind opt-in flags (and
pending additionally date-bounded, ADR-182 — later relaxed below by ADR-187
so stragglers could be caught, which this ADR subsumes). That stopped pending rows drifting
onto statements, but it did it by removing them from the screen — so an account
whose rows are all pending reconciled against an **empty table**, with a banner
explaining what it was refusing to show.

ADR-189 splits the two ideas apart:

- **Listing** is unconditional. Every row not already reconciled onto some
  *other* statement is a candidate, whatever its status and whatever its date.
  Rows ticked into the statement being resumed/viewed are listed too, so their
  ticks survive.
- **Pre-selection** carries the safety. Nothing below ``matched`` is ever
  ticked for you; a pending row reaches a statement only by a deliberate click.
  That half is asserted in ``test_reconcile_preselection.py``, which drives the
  real wizard.

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


def _open_statement(repo, acct, txn_ids=()):
    sid = repo._conn.execute(
        "INSERT INTO statement (iri, account_id, start_date, end_date, "
        " starting_balance_pence, ending_balance_pence, status) "
        "VALUES ('s:1', ?, '2026-06-01', '2026-06-30', 0, 0, 'open')",
        (acct,),
    ).lastrowid
    for tid in txn_ids:
        repo._conn.execute(
            "INSERT INTO statement_txn (statement_id, txn_id) VALUES (?, ?)",
            (sid, tid),
        )
    return sid


# ── listing is unconditional ────────────────────────────────────────────────


def test_every_unreconciled_row_is_a_candidate():
    """The headline of ADR-189, and the bug that prompted it: with no flags at
    all, pending and cleared rows are still listed. Under ADR-130 this returned
    the matched row alone, which is how a hand-kept account got an empty
    reconcile screen."""
    repo, acct, ids = _build()
    cand = _cand(repo, acct)
    assert ids["pending"] in cand
    assert ids["cleared"] in cand
    assert ids["matched"] in cand


def test_pending_is_listed_at_any_date():
    """Pending used to be visible only inside the statement dates. Nothing is
    date-bounded now — the period governs pre-selection, not listing — so a
    row dated well outside any plausible statement is still tickable."""
    repo, acct, ids = _build()
    far = repo.insert_transaction(
        account_id=acct, posted_date="2027-11-30", amount=Decimal("-5.00"),
        payee_id=None, category_id=1, status="pending", memo="",
        import_hash=None, import_batch_id=None,
    )
    assert far in _cand(repo, acct)


def test_reconciled_rows_are_excluded():
    """The one exclusion left: a row already tied to another statement is
    settled and must not be re-tickable onto this one."""
    repo, acct, ids = _build()
    assert ids["reconciled"] not in _cand(repo, acct)


def test_this_statements_rows_are_included_even_though_reconciled():
    """Resuming or viewing a statement must show its own ticks, or closing it
    again would silently drop them."""
    repo, acct, ids = _build()
    sid = _open_statement(repo, acct, [ids["reconciled"]])
    cand = _cand(repo, acct, include_statement_id=sid)
    assert ids["reconciled"] in cand


def test_other_statements_rows_stay_excluded():
    """Scoping check: including *this* statement's rows must not drag in rows
    reconciled onto a different one."""
    repo, acct, ids = _build()
    other = _open_statement(repo, acct, [ids["reconciled"]])
    mine = repo._conn.execute(
        "INSERT INTO statement (iri, account_id, start_date, end_date, "
        " starting_balance_pence, ending_balance_pence, status) "
        "VALUES ('s:2', ?, '2026-07-01', '2026-07-31', 0, 0, 'open')",
        (acct,),
    ).lastrowid
    assert other != mine
    assert ids["reconciled"] not in _cand(repo, acct, include_statement_id=mine)


def test_other_accounts_rows_are_excluded():
    repo, acct, ids = _build()
    other_acct = repo.create_account(
        name="Savings", type_key="savings", currency="GBP",
    ).id
    stray = repo.insert_transaction(
        account_id=other_acct, posted_date="2026-06-15", amount=Decimal("-1.00"),
        payee_id=None, category_id=1, status="pending", memo="",
        import_hash=None, import_batch_id=None,
    )
    assert stray not in _cand(repo, acct)


def test_the_gating_kwargs_are_gone():
    """ADR-189 removed ``include_cleared`` / ``include_pending`` / ``period``
    rather than leaving them as no-ops — a parameter that still accepts the old
    argument but ignores it is worse than one that is gone, because calling
    code keeps claiming a filter that no longer happens."""
    repo, acct, ids = _build()
    for kwargs in (
        {"include_cleared": True},
        {"include_pending": True},
        {"period": ("2026-06-01", "2026-06-30")},
    ):
        try:
            repo.list_reconcilable_txns(acct, **kwargs)
        except TypeError:
            continue
        raise AssertionError(f"{kwargs} should no longer be accepted")


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
