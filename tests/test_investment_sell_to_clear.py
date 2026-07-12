"""ADR-155 — Sell to clear: a closing sale lands the position on exactly zero.

The bug this guards against is subtle and is *not* an exception: the holdings
engine drains FIFO lots with ``while remaining > _EPS and queue``, so a sell
bigger than the holding stops at zero and silently drops the excess. Sell a
rounded 1167 against 1166.597 held and the register shows nothing wrong — but
the realised gain is overstated by the unbacked shares, and a compensating
ShrsIn "plug" then materialises a phantom position out of nothing.

So the assertions here are about *exactness*: the stored quantity is the
engine's own figure, and the position is gone from the holdings view afterwards
— not "small", gone.
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

from mfl_desktop.db.repository import Repository  # noqa: E402
from mfl_desktop.holdings import compute_holdings_view, shares_held  # noqa: E402


def _repo_with_account():
    db = Path(tempfile.mkdtemp(prefix="mfl_clear_")) / "m.mfl"
    repo = Repository(db)
    repo.create_account(
        name="Brokerage", type_key="investment", currency="USD",
        opening_balance=Decimal("0"),
    )
    return repo, repo.list_accounts()[0]


def _dialog(repo, acct, seed=None):
    from PySide6.QtWidgets import QApplication
    from mfl_desktop.ui.investment_transaction_dialog import InvestmentTransactionDialog

    QApplication.instance() or QApplication([])
    return InvestmentTransactionDialog(repo, acct, seed=seed)


def _save_answering_archive(dlg, *, retire: bool = False):
    """Save a close-out with the 'stop tracking this security?' prompt answered.

    A close-out ends in a modal question (ADR-155). Offscreen Qt still *blocks*
    on `exec`, so a test that doesn't answer it hangs rather than fails — stub
    it, and assert on what was asked."""
    from PySide6.QtWidgets import QMessageBox

    asked: list[str] = []
    original = QMessageBox.question

    def _answer(_parent, title, text, *a, **k):
        asked.append(f"{title}\n{text}")
        return QMessageBox.Yes if retire else QMessageBox.No

    QMessageBox.question = _answer
    try:
        dlg._on_save()
    finally:
        QMessageBox.question = original
    return asked


def _buy(repo, acct, sid, date, qty, price):
    repo.insert_transaction(
        account_id=acct.id, posted_date=date,
        amount=Decimal(str(round(-qty * price, 2))),
        payee_id=None, category_id=repo.uncategorised_id(), status="cleared",
        memo="", import_hash=None, import_batch_id=None,
        action="Buy", security_id=sid, quantity=qty, price=price,
        commission=None, accrued_interest=None,
    )
    repo.commit()


def _held(repo, acct, sid) -> float:
    return shares_held(repo.list_transactions_for_account(acct.id), sid)


def _seed_awkward_holding(repo, acct):
    """The real shape of the owner's data: years of dividend re-investment
    leaving a holding that is nobody's round number (1166.597)."""
    sid = repo.get_or_create_security("Vanguard Intl Div", "VWID")
    _buy(repo, acct, sid, "2021-02-11", 1000.0, 27.93)
    _buy(repo, acct, sid, "2022-06-29", 160.597, 24.41)
    _buy(repo, acct, sid, "2024-09-27", 6.0, 29.117)
    return sid


# ── the exact-quantity contract ────────────────────────────────────────────


def test_close_position_sells_the_exact_holding_not_a_rounded_one():
    repo, acct = _repo_with_account()
    sid = _seed_awkward_holding(repo, acct)
    assert abs(_held(repo, acct, sid) - 1166.597) < 1e-9

    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Sell"))
    dlg._security.setCurrentIndex(dlg._security.findData(sid))
    dlg._close_position.setChecked(True)
    # The user types the statement's cash, never a share count.
    dlg._total.setText("44600.78")
    _save_answering_archive(dlg)

    sells = [
        t for t in repo.list_transactions_for_account(acct.id)
        if (t.action or "") == "Sell"
    ]
    assert len(sells) == 1, "expected exactly one Sell row"
    assert abs(sells[0].quantity - 1166.597) < 1e-9, (
        f"sold {sells[0].quantity}, not the 1166.597 held"
    )
    assert sells[0].amount == Decimal("44600.78"), "proceeds must be the cash typed"


def test_the_position_is_gone_from_the_holdings_view():
    repo, acct = _repo_with_account()
    sid = _seed_awkward_holding(repo, acct)

    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Sell"))
    dlg._security.setCurrentIndex(dlg._security.findData(sid))
    dlg._close_position.setChecked(True)
    dlg._total.setText("44600.78")
    _save_answering_archive(dlg)

    view = compute_holdings_view(
        repo.list_transactions_for_account(acct.id), Decimal("0"), {},
    )
    assert not [h for h in view.holdings if h.security_id == sid], (
        "the closed position is still showing as a holding"
    )
    assert _held(repo, acct, sid) == 0.0


def test_realised_gain_is_proceeds_minus_the_whole_basis():
    repo, acct = _repo_with_account()
    sid = _seed_awkward_holding(repo, acct)
    basis = 1000.0 * 27.93 + 160.597 * 24.41 + 6.0 * 29.117   # 32,000.66

    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Sell"))
    dlg._security.setCurrentIndex(dlg._security.findData(sid))
    dlg._close_position.setChecked(True)
    dlg._total.setText("44600.78")
    _save_answering_archive(dlg)

    view = compute_holdings_view(
        repo.list_transactions_for_account(acct.id), Decimal("0"), {},
    )
    expected = Decimal(str(round(44600.78 - basis, 2)))
    assert abs(view.total_realized_gain - expected) <= Decimal("0.02"), (
        f"realised {view.total_realized_gain}, expected ~{expected}"
    )


def test_quantity_is_locked_and_the_price_is_backed_out_of_the_proceeds():
    repo, acct = _repo_with_account()
    sid = _seed_awkward_holding(repo, acct)

    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Sell"))
    dlg._security.setCurrentIndex(dlg._security.findData(sid))
    dlg._close_position.setChecked(True)
    assert dlg._qty.isReadOnly(), "the close-out quantity must not be editable"

    dlg._total.setText("44600.78")
    price = float(dlg._price.text())
    assert abs(price - 44600.78 / 1166.597) < 1e-4, f"price backed out wrong: {price}"

    label = dlg._form.labelForField(dlg._total).text()
    assert label == "Proceeds:", f"total row should read Proceeds, got {label!r}"


def test_typing_a_price_moves_the_proceeds_and_never_the_quantity():
    """The solver's third leg is normally free to be any of the three. With the
    quantity pinned, a price edit must retarget onto the proceeds — otherwise
    the solver would quietly overwrite the holding we're trying to clear."""
    repo, acct = _repo_with_account()
    sid = _seed_awkward_holding(repo, acct)

    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Sell"))
    dlg._security.setCurrentIndex(dlg._security.findData(sid))
    dlg._close_position.setChecked(True)
    dlg._total.setText("44600.78")
    dlg._price.setText("38.0")

    assert abs(float(dlg._qty.text()) - 1166.597) < 1e-6, "quantity was overwritten"
    assert abs(float(dlg._total.text()) - 1166.597 * 38.0) < 0.01, (
        "proceeds should have re-solved from the new price"
    )


def test_unticking_hands_the_quantity_back():
    repo, acct = _repo_with_account()
    sid = _seed_awkward_holding(repo, acct)

    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Sell"))
    dlg._security.setCurrentIndex(dlg._security.findData(sid))
    dlg._close_position.setChecked(True)
    dlg._close_position.setChecked(False)

    assert not dlg._qty.isReadOnly()
    assert dlg._form.labelForField(dlg._total).text() == "Total cost:"


# ── scoping: date, account, other actions ──────────────────────────────────


def test_the_quantity_is_the_holding_as_of_the_transaction_date():
    """Back-filling a closure sells what you held *then*, not what a later buy
    added — otherwise entering an old closure would sell shares you only bought
    afterwards."""
    from PySide6.QtCore import QDate

    repo, acct = _repo_with_account()
    sid = repo.get_or_create_security("Test Fund", "TFND")
    _buy(repo, acct, sid, "2024-01-10", 100.0, 10.0)
    _buy(repo, acct, sid, "2026-01-10", 50.0, 12.0)   # after the closure date

    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Sell"))
    dlg._security.setCurrentIndex(dlg._security.findData(sid))
    dlg._date.setDate(QDate(2025, 6, 30))
    dlg._close_position.setChecked(True)

    assert abs(float(dlg._qty.text()) - 100.0) < 1e-9, (
        "sold the post-dated buy as well as the holding at the sale date"
    )


def test_editing_a_saved_close_out_does_not_halve_the_quantity():
    """Re-opening a Sell-to-clear must exclude that row's own shares from the
    holding, or the second save would sell half of nothing."""
    repo, acct = _repo_with_account()
    sid = _seed_awkward_holding(repo, acct)

    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Sell"))
    dlg._security.setCurrentIndex(dlg._security.findData(sid))
    dlg._close_position.setChecked(True)
    dlg._total.setText("44600.78")
    _save_answering_archive(dlg)

    sell = [
        t for t in repo.list_transactions_for_account(acct.id)
        if (t.action or "") == "Sell"
    ][0]
    dlg2 = _dialog(repo, acct, seed=sell)
    dlg2._close_position.setChecked(True)

    assert abs(float(dlg2._qty.text()) - 1166.597) < 1e-6, (
        "the row's own sale was counted against the holding"
    )


def test_the_checkbox_is_sell_only_and_clears_on_switching_action():
    repo, acct = _repo_with_account()
    sid = _seed_awkward_holding(repo, acct)

    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Sell"))
    dlg._security.setCurrentIndex(dlg._security.findData(sid))
    assert dlg._close_position.isVisibleTo(dlg)
    dlg._close_position.setChecked(True)

    dlg._action.setCurrentIndex(dlg._action.findData("Buy"))
    assert not dlg._close_position.isVisibleTo(dlg)
    assert not dlg._close_position.isChecked(), "a hidden lock the user can't undo"
    assert not dlg._qty.isReadOnly(), "quantity left locked on a Buy"


def test_clearing_nothing_is_refused():
    repo, acct = _repo_with_account()
    sid = repo.get_or_create_security("Never Held", "NONE")

    dlg = _dialog(repo, acct)
    dlg._action.setCurrentIndex(dlg._action.findData("Sell"))
    dlg._security.setCurrentIndex(dlg._security.findData(sid))

    from PySide6.QtWidgets import QMessageBox
    warned = []
    original = QMessageBox.warning
    QMessageBox.warning = lambda *a, **k: warned.append(a) or QMessageBox.Ok
    try:
        dlg._close_position.setChecked(True)
    finally:
        QMessageBox.warning = original

    assert warned, "expected a warning when there's nothing to clear"
    assert not dlg._close_position.isChecked(), "the tick should not have stuck"


# ── retire / restore the security ──────────────────────────────────────────


def test_archive_hides_the_security_and_stops_it_being_priced():
    repo, acct = _repo_with_account()
    sid = _seed_awkward_holding(repo, acct)

    assert sid in {s.id for s in repo.list_securities()}
    repo.archive_security(sid)
    repo.commit()

    assert sid not in {s.id for s in repo.list_securities()}, "retired but still listed"
    assert sid in {s.id for s in repo.list_securities(include_archived=True)}
    assert sid not in {s.id for s in repo.securities_to_price()}, (
        "a retired security is still costing a price request"
    )

    repo.unarchive_security(sid)
    repo.commit()
    assert sid in {s.id for s in repo.list_securities()}, "restore did not put it back"


def test_archiving_keeps_the_transactions_and_the_realised_gain():
    """Retiring is a display/pricing gate, not a delete — the history and the
    gain it produced must survive it."""
    repo, acct = _repo_with_account()
    sid = _seed_awkward_holding(repo, acct)
    before = len(repo.list_transactions_for_account(acct.id))

    repo.archive_security(sid)
    repo.commit()

    after = repo.list_transactions_for_account(acct.id)
    assert len(after) == before, "retiring a security touched its transactions"
    view = compute_holdings_view(after, Decimal("0"), {})
    assert view.total_cost_basis > 0 or view.total_realized_gain != 0


def _run_all() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {e}")
    print(f"\n{'FAILED' if failures else 'OK'} — {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
