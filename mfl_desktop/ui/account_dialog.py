"""Modal dialog for creating or editing an account.

Used in two modes:
- Create: type is editable; submitting returns the values for the caller to
  pass to Repository.create_account.
- Edit: type is locked (changing family / liability would reinterpret stored
  amounts); submitting returns the values for Repository.update_account.

Opening balance accepts a signed Decimal — useful for credit-card accounts
which often open with a debit balance.
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
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from mfl_desktop.account_types import ACCOUNT_TYPES, by_storage
from mfl_desktop.db.repository import AccountSummary


@dataclass(frozen=True)
class AccountDialogValues:
    name: str
    type_key: Optional[str]   # short key ('cash' etc.); None when editing
    currency: str             # uppercased ISO 4217 code
    opening_balance: Decimal


class AccountDialog(QDialog):
    """Single dialog handling both create and edit modes."""

    def __init__(
        self,
        existing: Optional[AccountSummary] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._existing = existing
        is_edit = existing is not None
        self.setWindowTitle("Edit Account" if is_edit else "New Account")
        self.setModal(True)

        # ── widgets ──

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. Joint Current Account")

        self._type_combo = QComboBox()
        for spec in ACCOUNT_TYPES:
            self._type_combo.addItem(spec.label, userData=spec.key)

        if is_edit:
            # Lock type — changing family / liability would reinterpret amounts.
            spec = by_storage(existing.type)
            for i in range(self._type_combo.count()):
                if self._type_combo.itemData(i) == spec.key:
                    self._type_combo.setCurrentIndex(i)
                    break
            self._type_combo.setEnabled(False)
            self._type_combo.setToolTip(
                "Type can't be changed after creation — family and liability "
                "semantics affect every stored amount. To change type, delete "
                "this account and create a new one."
            )

        self._currency_edit = QLineEdit()
        self._currency_edit.setMaxLength(3)
        self._currency_edit.setPlaceholderText("GBP")
        # Force uppercase as the user types.
        self._currency_edit.textChanged.connect(self._uppercase_currency)

        self._opening_edit = QLineEdit()
        self._opening_edit.setAlignment(Qt.AlignRight)
        self._opening_edit.setPlaceholderText("0.00")
        validator = QDoubleValidator(-1_000_000_000.0, 1_000_000_000.0, 2, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._opening_edit.setValidator(validator)

        # Prefill in edit mode.
        if is_edit:
            self._name_edit.setText(existing.name)
            self._currency_edit.setText(existing.currency)
            self._opening_edit.setText(f"{existing.opening_balance:.2f}")
        else:
            self._currency_edit.setText("GBP")
            self._opening_edit.setText("0.00")

        # ── layout ──

        form = QFormLayout()
        form.addRow("Name:", self._name_edit)
        form.addRow("Type:", self._type_combo)
        form.addRow("Currency:", self._currency_edit)
        form.addRow("Opening balance:", self._opening_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.resize(400, self.sizeHint().height())
        self._values: Optional[AccountDialogValues] = None

    # ── helpers ──

    def _uppercase_currency(self, text: str) -> None:
        upper = text.upper()
        if upper != text:
            cursor = self._currency_edit.cursorPosition()
            self._currency_edit.blockSignals(True)
            self._currency_edit.setText(upper)
            self._currency_edit.setCursorPosition(cursor)
            self._currency_edit.blockSignals(False)

    def _on_accept(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required", "Enter an account name.")
            return

        currency = self._currency_edit.text().strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            QMessageBox.warning(
                self, "Invalid currency",
                "Currency must be a 3-letter ISO code (e.g. GBP, USD, EUR).",
            )
            return

        raw = self._opening_edit.text().strip().replace(",", "")
        if not raw:
            opening = Decimal("0.00")
        else:
            try:
                opening = Decimal(raw)
            except InvalidOperation:
                QMessageBox.warning(
                    self, "Invalid opening balance",
                    f"Could not parse {raw!r} as a number.",
                )
                return

        type_key = (
            None if self._existing is not None
            else self._type_combo.currentData()
        )
        self._values = AccountDialogValues(
            name=name, type_key=type_key,
            currency=currency, opening_balance=opening,
        )
        self.accept()

    def values(self) -> Optional[AccountDialogValues]:
        return self._values
