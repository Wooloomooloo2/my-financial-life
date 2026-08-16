"""Bulk-editing an already-paired transfer's category doesn't re-prompt (ADR-190).

Owner-reported from a screenshot: a dozen Ikea Credit Card rows, every one
already paired to Chase Checking by Transfer Reconcile ("Transfer from Chase
Checking", status Matched). Bulk-editing them onto a *different* transfer
category popped "You picked a transfer category. Which account is the other
side for these transactions?" with Ally Savings pre-filled — asking again for
something already answered, and pointing at the wrong account.

The inline single-row category edit has always returned early on
``transfer_id`` (``register_window.py``, the ``_on_data_changed`` guard). The
bulk path never learned that: it branched on the *category kind* alone and ran
the whole destination + matcher + review flow over every selected row.

What that actually did, which is worth recording because it is not what it
looks like: the repository refuses to re-pair. ``_convert_to_transfer_unbatched``
raises "already part of a transfer" and ``_link_transfer_unbatched`` raises
"Source is already part of a transfer", and ``bulk_match_or_create_transfers``
rolls the whole batch back on either. So no duplicate partner was ever written.
The damage was a needless question, then a hard failure that also discarded the
pairing work for any rows in the same selection that *did* need it — while the
phase-1 category update, committed separately before the review, stuck.

These tests cover both halves: the repository's refusal (the reason this was
never data corruption) and the register's partition (the reason it no longer
asks).

Run: ``.venv/bin/pytest tests/test_bulk_edit_keeps_existing_transfer.py``
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.register_window import RegisterWindow


def _build():
    """A card whose rows are already transfers to checking, plus one plain row
    — the mixed selection the fix has to split."""
    db = Path(tempfile.mkdtemp(prefix="mfl_bulkxfer_")) / "money.mfl"
    repo = Repository(db)
    card = repo.create_account(
        name="Ikea Credit Card", type_key="credit", currency="USD",
    )
    checking = repo.create_account(
        name="Chase Checking", type_key="cash", currency="USD",
    )
    savings = repo.create_account(
        name="Ally Savings", type_key="savings", currency="USD",
    )
    # "Transfer" is seeded; add a second transfer category to move rows onto.
    xfer_a = repo.get_default_transfer_category_id()
    xfer_b = repo.create_category("Card Payment", None, "transfer")

    paired = []
    for i, amount in enumerate(("69.08", "104.09", "746.34")):
        repo.create_transfer(
            from_account_id=checking.id, to_account_id=card.id,
            posted_date=f"2024-02-1{i}", amount=Decimal(amount),
            category_id=xfer_a, status="matched",
        )
    for r in repo.list_transactions_for_account(card.id):
        if r.transfer_id is not None:
            paired.append(r.id)

    plain = repo.insert_transaction(
        account_id=card.id, posted_date="2024-02-20",
        amount=Decimal("-25.00"), payee_id=None, category_id=xfer_a,
        status="matched", memo="", import_hash=None, import_batch_id=None,
    )
    repo.commit()
    return repo, card, checking, savings, xfer_a, xfer_b, paired, plain


# ── the repository refuses to re-pair (why this was never duplication) ──────


def test_repo_refuses_to_convert_an_already_paired_row():
    repo, card, checking, savings, xa, xb, paired, plain = _build()
    with pytest.raises(ValueError, match="already part of a transfer"):
        repo.convert_to_transfer(
            txn_id=paired[0], other_account_id=savings.id,
        )


def test_repo_refuses_to_link_an_already_paired_row():
    repo, card, checking, savings, xa, xb, paired, plain = _build()
    other = repo.insert_transaction(
        account_id=savings.id, posted_date="2024-02-11",
        amount=Decimal("-69.08"), payee_id=None, category_id=xa,
        status="matched", memo="", import_hash=None, import_batch_id=None,
    )
    repo.commit()
    with pytest.raises(ValueError, match="already part of a transfer"):
        repo.link_transfer(
            source_txn_id=paired[0], candidate_txn_id=other, category_id=xa,
        )


def test_no_extra_partner_was_ever_written():
    """The count on the target account is the thing the owner was worried
    about. It does not move, even on the failing path."""
    repo, card, checking, savings, xa, xb, paired, plain = _build()
    before = len(repo.list_transactions_for_account(savings.id))
    try:
        repo.convert_to_transfer(
            txn_id=paired[0], other_account_id=savings.id,
        )
    except ValueError:
        pass
    assert len(repo.list_transactions_for_account(savings.id)) == before


# ── the register no longer asks ─────────────────────────────────────────────


def _open(repo, account):
    """A register on ``account`` showing full history — the default is a
    12-month window (ADR-041), which would hide the fixture's older rows and
    make every selection empty (and every assertion here vacuous)."""
    win = RegisterWindow(repo, account.iri)
    win._window_key = "all"
    win._model.set_since(win._effective_since())   # what the Show combo does
    return win


def _select(win, txn_ids):
    """Select the model rows carrying ``txn_ids`` in the register table."""
    from PySide6.QtCore import QItemSelectionModel
    sel = win._table.selectionModel()
    sel.clearSelection()
    wanted = set(txn_ids)
    for i in range(win._model.rowCount()):
        if win._model.row_at(i).id in wanted:
            proxy_idx = win._proxy.mapFromSource(win._model.index(i, 0))
            if proxy_idx.isValid():
                sel.select(
                    proxy_idx,
                    QItemSelectionModel.Select | QItemSelectionModel.Rows,
                )
    return sel


def _patch_dialogs(monkeypatch, category_id, destination_id, asked):
    """Stub every modal the bulk path can raise.

    The review dialog is stubbed even in the tests that should never reach it:
    against the unfixed code they DO reach it, and an unstubbed modal blocks
    the run forever instead of failing. A test whose failure mode is a hang
    cannot tell you what broke.
    """
    monkeypatch.setattr(
        RegisterWindow, "_prompt_destination_account",
        lambda self, **kw: asked.append(kw) or destination_id,
    )
    monkeypatch.setattr(
        "mfl_desktop.ui.register_window.BulkEditDialog.exec", lambda self: True,
    )
    monkeypatch.setattr(
        "mfl_desktop.ui.register_window.BulkEditDialog.values",
        lambda self: {"category_id": category_id},
    )
    monkeypatch.setattr(
        "mfl_desktop.ui.register_window.BulkTransferReviewDialog.exec",
        lambda self: False,
    )


def test_all_paired_selection_takes_the_no_prompt_path(monkeypatch):
    """The reported case: every selected row already has a partner, so the
    destination picker must never open and the categories just change."""
    repo, card, checking, savings, xa, xb, paired, plain = _build()
    win = _open(repo, card)
    _select(win, paired)

    asked = []
    _patch_dialogs(monkeypatch, xb, savings.id, asked)
    win._on_bulk_edit()

    assert asked == [], "the destination picker was opened for paired rows"
    for tid in paired:
        row = next(
            r for r in repo.list_transactions_for_account(card.id)
            if r.id == tid
        )
        assert row.category_id == xb           # recategorised
        assert row.transfer_id is not None     # pairing intact


def test_paired_rows_keep_their_original_partner(monkeypatch):
    repo, card, checking, savings, xa, xb, paired, plain = _build()
    before = {
        tid: repo.get_transfer_partner_account_id(tid) for tid in paired
    }
    win = _open(repo, card)
    _select(win, paired)
    _patch_dialogs(monkeypatch, xb, savings.id, [])
    win._on_bulk_edit()

    after = {tid: repo.get_transfer_partner_account_id(tid) for tid in paired}
    assert after == before
    assert all(v == checking.id for v in after.values())


def test_mixed_selection_still_prompts_but_only_for_the_unpaired(monkeypatch):
    """A selection with one unpaired row still needs a destination — for that
    row alone. The paired ones must not reach the pairing batch, which is what
    used to roll the whole thing back."""
    repo, card, checking, savings, xa, xb, paired, plain = _build()
    win = _open(repo, card)
    _select(win, paired + [plain])

    asked = []
    _patch_dialogs(monkeypatch, xb, savings.id, asked)
    # Decline the review dialog: the pairing batch never runs, but we can still
    # assert what the prompt was told and that phase 1 applied to everything.
    monkeypatch.setattr(
        "mfl_desktop.ui.register_window.BulkTransferReviewDialog.exec",
        lambda self: False,
    )
    win._on_bulk_edit()

    assert len(asked) == 1
    # The card is the only unpaired row's account, so it is excluded; the
    # already-paired rows' partner account stays available as a destination.
    assert asked[0]["exclude_account_ids"] == {card.id}
    assert "1 transactions" in asked[0]["message"]
    assert "3 of the selected transactions are already transfers" in \
        asked[0]["message"]

    rows = {r.id: r for r in repo.list_transactions_for_account(card.id)}
    for tid in paired:
        assert rows[tid].category_id == xb
        assert rows[tid].transfer_id is not None


def test_unpaired_only_selection_is_unchanged(monkeypatch):
    """The regression guard: with nothing pre-paired the flow behaves exactly
    as it did — prompt, with no 'already transfers' note."""
    repo, card, checking, savings, xa, xb, paired, plain = _build()
    second_plain = repo.insert_transaction(
        account_id=card.id, posted_date="2024-02-21",
        amount=Decimal("-30.00"), payee_id=None, category_id=xa,
        status="matched", memo="", import_hash=None, import_batch_id=None,
    )
    repo.commit()
    win = _open(repo, card)
    _select(win, [plain, second_plain])

    asked = []
    _patch_dialogs(monkeypatch, xb, savings.id, asked)
    win._on_bulk_edit()

    assert len(asked) == 1
    assert "already transfers" not in asked[0]["message"]
