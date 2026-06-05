"""Modal dialog for entering a new manual transaction.

Used by the register's "New Transaction" action. Collects the fields the
Repository needs to insert a row, validates them, and exposes the result
via :meth:`NewTransactionDialog.values` for the caller to commit.

Sign convention: the UI presents a direction toggle (Money out / Money in)
plus a positive amount; the dialog returns a signed Decimal (negative for
money out) to match the schema's signed `txn.amount`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
)

from mfl_desktop.db.repository import AccountSummary, CategoryChoice
from mfl_desktop.ui.category_picker import (
    make_category_picker,
    selected_category_id,
)

STATUSES = ("Pending", "Uncleared", "Cleared", "Reconciled")


@dataclass(frozen=True)
class NewTransactionValues:
    """The validated form values, ready to hand to Repository.insert_transaction."""
    account_id: int
    posted_date: str       # ISO 'YYYY-MM-DD'
    amount: Decimal        # signed; negative for money out
    payee_name: str        # may be empty
    category_id: int
    status: str
    memo: str              # may be empty


class NewTransactionDialog(QDialog):
    def __init__(
        self,
        accounts: list[AccountSummary],
        categories: list[CategoryChoice],
        default_account_id: Optional[int] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Transaction")
        self.setModal(True)
        self._accounts = accounts
        self._categories = categories

        # ── widgets ──

        self._account_combo = QComboBox()
        for acct in accounts:
            self._account_combo.addItem(
                f"{acct.name}  ·  {acct.currency}", userData=acct.id,
            )
        if default_account_id is not None:
            for i in range(self._account_combo.count()):
                if self._account_combo.itemData(i) == default_account_id:
                    self._account_combo.setCurrentIndex(i)
                    break

        self._date_edit = QDateEdit()
        self._date_edit.setDisplayFormat("yyyy-MM-dd")
        self._date_edit.setCalendarPopup(True)
        self._date_edit.setDate(QDate.currentDate())

        self._payee_edit = QLineEdit()
        self._payee_edit.setPlaceholderText("Optional")

        # Searchable dropdown — same helper used in BulkEditDialog so the
        # two surfaces behave identically. Default to Uncategorised (id=1).
        self._category_combo = make_category_picker(categories, default_id=1)

        self._status_combo = QComboBox()
        self._status_combo.addItems(STATUSES)
        self._status_combo.setCurrentText("Pending")

        # Direction: two radio buttons in a button group so exactly one is set.
        self._direction_out = QRadioButton("Money out")
        self._direction_in = QRadioButton("Money in")
        self._direction_out.setChecked(True)
        direction_group = QButtonGroup(self)
        direction_group.addButton(self._direction_out)
        direction_group.addButton(self._direction_in)
        direction_row = QHBoxLayout()
        direction_row.setContentsMargins(0, 0, 0, 0)
        direction_row.addWidget(self._direction_out)
        direction_row.addWidget(self._direction_in)
        direction_row.addStretch(1)

        self._amount_edit = QLineEdit()
        self._amount_edit.setPlaceholderText("0.00")
        self._amount_edit.setAlignment(Qt.AlignRight)
        validator = QDoubleValidator(0.0, 1_000_000_000.0, 2, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._amount_edit.setValidator(validator)

        self._memo_edit = QLineEdit()
        self._memo_edit.setPlaceholderText("Optional")

        # ── layout ──

        form = QFormLayout()
        form.addRow("Account:", self._account_combo)
        form.addRow("Date:", self._date_edit)
        form.addRow("Payee:", self._payee_edit)
        form.addRow("Category:", self._category_combo)
        form.addRow("Status:", self._status_combo)
        form.addRow("Direction:", self._wrap(direction_row))
        form.addRow("Amount:", self._amount_edit)
        form.addRow("Memo:", self._memo_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.resize(420, self.sizeHint().height())
        self._values: Optional[NewTransactionValues] = None

    # ── helpers ──

    @staticmethod
    def _wrap(inner_layout) -> "QHBoxLayout":
        """Wrap a layout so QFormLayout.addRow accepts it as a field widget."""
        from PySide6.QtWidgets import QWidget
        w = QWidget()
        w.setLayout(inner_layout)
        return w

    def _on_accept(self) -> None:
        # Account
        account_id = self._account_combo.currentData()
        if account_id is None:
            QMessageBox.warning(self, "Account required", "Pick an account.")
            return

        # Category — searchable combo, so validate via the helper that
        # checks the typed text matches a real item rather than just
        # trusting currentData (which may reflect a stale prior selection).
        category_id = selected_category_id(self._category_combo)
        if category_id is None:
            QMessageBox.warning(
                self, "Category required",
                "Pick a category from the list.",
            )
            return

        # Amount: positive Decimal, then sign by direction.
        raw = self._amount_edit.text().strip().replace(",", "")
        if not raw:
            QMessageBox.warning(self, "Amount required", "Enter an amount.")
            return
        try:
            magnitude = Decimal(raw)
        except InvalidOperation:
            QMessageBox.warning(self, "Invalid amount", f"Could not parse {raw!r}.")
            return
        if magnitude <= 0:
            QMessageBox.warning(
                self, "Invalid amount",
                "Amount must be greater than zero — use Direction to choose "
                "money out vs money in.",
            )
            return
        amount = -magnitude if self._direction_out.isChecked() else magnitude

        # Date — QDateEdit always returns a valid QDate.
        qd: QDate = self._date_edit.date()
        posted_date = date(qd.year(), qd.month(), qd.day()).isoformat()

        self._values = NewTransactionValues(
            account_id=int(account_id),
            posted_date=posted_date,
            amount=amount,
            payee_name=self._payee_edit.text().strip(),
            category_id=int(category_id),
            status=self._status_combo.currentText(),
            memo=self._memo_edit.text().strip(),
        )
        self.accept()

    def values(self) -> Optional[NewTransactionValues]:
        """Return the validated values, or None if the dialog was cancelled."""
        return self._values
