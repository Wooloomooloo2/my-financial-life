"""Destination picker for transfer creation, cross-currency aware.

Per ADR-035 §UI "Cross-currency transfer dialog" — when the user marks a
transaction as a transfer (New Transaction with a transfer-kind category,
inline category edit, or scheduled-txn post) the chosen other-side
account may differ in currency from the transaction's account. In that
case the user typically knows the exact amount that hit the receiving
account (e.g. from the inbound statement) and the rate is back-derived
from the two amounts. ADR-035 spec'd this surface; the foundation work
shipped without it and cross-currency flows were instead blocked on the
FX table having a stored rate. This dialog implements the missing
surface.

Single dialog covers both same-currency (just pick the account) and
cross-currency (pick + enter other-side amount) flows so call sites
don't have to fork. The dialog is framed around "this account" (the
register row's account) and "the other account" (the new other side),
so callers can use it symmetrically for inflows and outflows. Callers
translate the returned ``other_amount`` to the repository's
``amount`` / ``to_amount`` kwargs based on the row's sign.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.ui import tokens


_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€"}


def _fmt_signed(value: Decimal, currency: str) -> str:
    """Display helper — `-$50.00`, `+£12.34`, `EUR 7.50`."""
    sym = _SYMBOL.get(currency, "")
    sign = "-" if value < 0 else ("+" if value > 0 else "")
    body = f"{abs(value):,.2f}"
    if sym:
        return f"{sign}{sym}{body}"
    return f"{sign}{currency} {body}"


@dataclass(frozen=True)
class TransferDestinationChoice:
    """The dialog's result.

    - ``account_id`` is the picked other-side account.
    - ``other_amount`` is the magnitude (positive) in the *other*
      account's currency. ``None`` for same-currency transfers — the
      repository treats that as "matches this side's magnitude" and
      stamps ``transfer.rate_source`` as ``"derived"``.

    Callers translate to repository args:

    - For an outflow from this_account (signed amount < 0):
      ``amount=abs(this_amount), to_amount=other_amount`` and
      ``from_account_id=this_account.id, to_account_id=account_id``.
    - For an inflow into this_account (signed amount > 0):
      ``amount=other_amount, to_amount=abs(this_amount)`` and
      ``from_account_id=account_id, to_account_id=this_account.id``.

    Same-currency: pass ``other_amount=None`` to either side; the
    repository's same-currency early-exit applies.
    """
    account_id: int
    other_amount: Optional[Decimal]


class TransferDestinationDialog(QDialog):
    """Pick the other side of a transfer; if it's a different currency
    from this account, also collect the amount the other account will
    record.

    Layout (top → bottom):
    - intro paragraph
    - this-side summary line (read-only): account, signed amount, date
    - other-account combo
    - cross-currency block (hidden when same-currency):
        - other-amount field
        - implied-rate line (live)
        - hint line (pre-fill source / no-rate explainer)

    The cross-currency block is pre-filled from
    ``Repository.get_fx_rate_nearest`` when a rate exists; otherwise the
    field is blank and the user types the amount. The implied rate
    updates as the user edits.
    """

    def __init__(
        self,
        *,
        repo: Repository,
        source_account: AccountSummary,
        source_magnitude: Decimal,
        source_signed_display: Decimal,
        posted_date: str,
        exclude_account_ids: set[int],
        locked_account_id: Optional[int] = None,
        title: str = "Transfer destination",
        intro: str = "Which account is the other side of this transfer?",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        self._repo = repo
        # "source" naming in the ctor kwargs preserves call-site ergonomics
        # from earlier drafts; internally we treat it as "this account"
        # (the register row's account) and let the caller translate the
        # returned other_amount based on flow direction.
        self._this_account = source_account
        self._this_magnitude = source_magnitude
        self._this_signed = source_signed_display
        self._posted_date = posted_date
        self._result: Optional[TransferDestinationChoice] = None

        accounts = repo.list_accounts()
        if locked_account_id is not None:
            # Scheduled-txn flow locks the other account to the schedule's
            # transfer_to_account_id — only one candidate, combo disabled.
            candidates = [a for a in accounts if a.id == locked_account_id]
        else:
            candidates = [
                a for a in accounts if a.id not in exclude_account_ids
            ]
        self._candidates = candidates

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        intro_lbl = QLabel(intro)
        intro_lbl.setWordWrap(True)
        outer.addWidget(intro_lbl)

        # This-side summary — neutral framing, sign carries direction.
        this_html = (
            f"<b>{source_account.name}</b> · {posted_date} · "
            f"<span style='color:#0F172A'>"
            f"{_fmt_signed(source_signed_display, source_account.currency)}"
            f"</span>"
        )
        this_lbl = QLabel(this_html)
        this_lbl.setTextFormat(Qt.RichText)
        tokens.themed(this_lbl, "QLabel { color: {muted_strong}; }")
        outer.addWidget(this_lbl)

        # Other-account combo row.
        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Other account:"))
        self._combo = QComboBox(self)
        for a in candidates:
            self._combo.addItem(f"{a.name}  ·  {a.currency}", userData=a.id)
        if locked_account_id is not None:
            self._combo.setEnabled(False)
        dest_row.addWidget(self._combo, 1)
        outer.addLayout(dest_row)

        # Cross-currency block — built once, shown/hidden on combo change.
        self._fx_block = QWidget(self)
        fx_layout = QVBoxLayout(self._fx_block)
        fx_layout.setContentsMargins(0, 6, 0, 0)
        fx_layout.setSpacing(4)

        amt_row = QHBoxLayout()
        self._amount_label = QLabel("Amount on the other side:")
        amt_row.addWidget(self._amount_label)
        self._amount_field = QLineEdit(self._fx_block)
        validator = QDoubleValidator(0.0, 1_000_000_000.0, 4, self._amount_field)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._amount_field.setValidator(validator)
        self._amount_field.textChanged.connect(self._update_rate_display)
        amt_row.addWidget(self._amount_field, 1)
        fx_layout.addLayout(amt_row)

        self._rate_label = QLabel(" ")
        tokens.themed(self._rate_label, "QLabel { color: {muted_strong}; font-size: 11px; }")
        fx_layout.addWidget(self._rate_label)

        self._hint_label = QLabel(" ")
        tokens.themed(self._hint_label, "QLabel { color: {subtle}; font-size: 11px; }")
        self._hint_label.setWordWrap(True)
        fx_layout.addWidget(self._hint_label)

        outer.addWidget(self._fx_block)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._combo.currentIndexChanged.connect(self._on_destination_changed)
        if candidates:
            self._on_destination_changed()
        else:
            self._fx_block.hide()

    # ── reactions ──────────────────────────────────────────────────────

    def _selected_account(self) -> Optional[AccountSummary]:
        chosen_id = self._combo.currentData()
        for a in self._candidates:
            if a.id == chosen_id:
                return a
        return None

    def _on_destination_changed(self) -> None:
        chosen = self._selected_account()
        if chosen is None:
            self._fx_block.hide()
            return
        this_ccy = self._this_account.currency
        other_ccy = chosen.currency
        if this_ccy == other_ccy:
            self._fx_block.hide()
            return
        self._fx_block.show()
        self._amount_label.setText(f"Amount in {other_ccy}:")

        # Pre-fill from the FX table when a rate exists. Same lookup
        # chain the matcher uses, so weekend / pre-history cases
        # naturally surface the nearest-prior rate.
        rate, rate_date, was_fallback = self._repo.get_fx_rate_nearest(
            self._posted_date, this_ccy, other_ccy,
        )
        self._amount_field.blockSignals(True)
        if rate is not None and self._this_magnitude > 0:
            suggested = self._this_magnitude * rate
            self._amount_field.setText(f"{suggested:.2f}")
            if was_fallback:
                self._hint_label.setText(
                    f"Pre-filled from rate dated {rate_date} (no exact "
                    f"rate for {self._posted_date}). Edit if your "
                    f"statement shows a different amount."
                )
            else:
                self._hint_label.setText(
                    f"Pre-filled from stored rate ({rate_date}). Edit if "
                    f"your statement shows a different amount."
                )
        else:
            self._amount_field.clear()
            self._hint_label.setText(
                f"No stored {this_ccy} → {other_ccy} rate. Enter the "
                f"amount that hit the {other_ccy} account; the rate is "
                f"back-derived from the two amounts."
            )
        self._amount_field.blockSignals(False)
        self._update_rate_display()

    def _update_rate_display(self) -> None:
        chosen = self._selected_account()
        if chosen is None:
            self._rate_label.setText(" ")
            return
        this_ccy = self._this_account.currency
        if chosen.currency == this_ccy:
            self._rate_label.setText(" ")
            return
        text = self._amount_field.text().strip()
        try:
            other_amount = Decimal(text) if text else None
        except InvalidOperation:
            other_amount = None
        if (
            other_amount is None
            or other_amount <= 0
            or self._this_magnitude <= 0
        ):
            self._rate_label.setText(
                f"Implied rate: 1 {this_ccy} = ? {chosen.currency}"
            )
            return
        rate = other_amount / self._this_magnitude
        self._rate_label.setText(
            f"Implied rate: 1 {this_ccy} = {rate:.4f} {chosen.currency}"
        )

    def _on_accept(self) -> None:
        chosen = self._selected_account()
        if chosen is None:
            QMessageBox.warning(
                self, "Pick an account",
                "Choose the other account.",
            )
            return
        if chosen.currency == self._this_account.currency:
            self._result = TransferDestinationChoice(
                account_id=chosen.id, other_amount=None,
            )
            self.accept()
            return
        text = self._amount_field.text().strip()
        if not text:
            QMessageBox.warning(
                self, "Enter the other-side amount",
                f"Enter the amount in {chosen.currency} that the other "
                f"account will record.",
            )
            return
        try:
            other_amount = Decimal(text)
        except InvalidOperation:
            QMessageBox.warning(
                self, "Invalid amount", "Enter a numeric value.",
            )
            return
        if other_amount <= 0:
            QMessageBox.warning(
                self, "Invalid amount",
                "Amount must be greater than zero.",
            )
            return
        self._result = TransferDestinationChoice(
            account_id=chosen.id, other_amount=other_amount,
        )
        self.accept()

    def values(self) -> Optional[TransferDestinationChoice]:
        """The user's choice, or ``None`` if cancelled / no candidates."""
        return self._result


def no_other_accounts_message(parent: QWidget) -> None:
    """Shared 'need a second account' message used when the call site
    decides not to open the dialog at all."""
    QMessageBox.information(
        parent, "No other account",
        "You need at least one other account to record a transfer.",
    )
