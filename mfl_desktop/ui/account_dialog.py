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
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from mfl_desktop.account_types import ACCOUNT_TYPES, by_storage
from mfl_desktop.currencies import (
    ISO_4217_CODES,
    ISO_4217_CURRENCIES,
    currency_label,
)
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

        # Editable typeahead combo seeded with the ISO 4217 active-currency
        # list. The user types the code (or the currency name — the
        # MatchContains filter on the QCompleter matches against the
        # full "CODE — Name" label so typing "dollar" narrows to all
        # dollar variants) and picks. Free-text entries that aren't in
        # the list are rejected on save with a hint — see _on_accept.
        # If we're editing an existing account whose stored code isn't
        # in the current ISO list (legacy data from before this dialog
        # tightened), the existing code is added at the top as a one-off
        # so the dialog can round-trip without forcing the user to
        # re-pick on every save.
        self._currency_combo = QComboBox()
        self._currency_combo.setEditable(True)
        self._currency_combo.setInsertPolicy(QComboBox.NoInsert)
        existing_code = existing.currency.strip().upper() if is_edit else ""
        if existing_code and existing_code not in ISO_4217_CODES:
            self._currency_combo.addItem(currency_label(existing_code), existing_code)
        for code, _name in ISO_4217_CURRENCIES:
            self._currency_combo.addItem(currency_label(code), code)
        completer = self._currency_combo.completer()
        if completer is not None:
            completer.setCompletionMode(QCompleter.PopupCompletion)
            completer.setFilterMode(Qt.MatchContains)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
        # Uppercase the user's typing in the line-edit half so the
        # combo's match logic doesn't care about case ("usd" → "USD"
        # in place; the QCompleter filter is also case-insensitive so
        # this is belt-and-braces).
        line_edit = self._currency_combo.lineEdit()
        if line_edit is not None:
            line_edit.textChanged.connect(self._uppercase_currency)

        self._opening_edit = QLineEdit()
        self._opening_edit.setAlignment(Qt.AlignRight)
        self._opening_edit.setPlaceholderText("0.00")
        validator = QDoubleValidator(-1_000_000_000.0, 1_000_000_000.0, 2, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._opening_edit.setValidator(validator)

        # Prefill in edit mode.
        if is_edit:
            self._name_edit.setText(existing.name)
            self._set_currency(existing.currency)
            self._opening_edit.setText(f"{existing.opening_balance:.2f}")
        else:
            self._set_currency("GBP")
            self._opening_edit.setText("0.00")

        # ── layout ──

        form = QFormLayout()
        form.addRow("Name:", self._name_edit)
        form.addRow("Type:", self._type_combo)
        form.addRow("Currency:", self._currency_combo)
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

    def _set_currency(self, code: str) -> None:
        """Pre-select the combo to ``code``. If the code is in the ISO
        list (the normal case), the matching row is selected. If it's a
        legacy non-ISO code already on an existing account, the constructor
        added a one-off row at the top so this still selects it cleanly."""
        target = code.strip().upper()
        for i in range(self._currency_combo.count()):
            if self._currency_combo.itemData(i) == target:
                self._currency_combo.setCurrentIndex(i)
                return
        # Defensive: shouldn't happen because the ctor added a one-off
        # row for any non-ISO existing code, but if it does, fall back
        # to typing the code into the line-edit so the user sees what's
        # stored.
        self._currency_combo.setEditText(target)

    def _uppercase_currency(self, text: str) -> None:
        """Force the line-edit half of the combo to uppercase as the user
        types. The QCompleter's case-insensitive filter would still match
        either way, but uppercase-on-type makes the field's visual state
        match the stored code, which avoids a "did I save 'gbp' or 'GBP'?"
        moment when the dialog re-opens."""
        line_edit = self._currency_combo.lineEdit()
        if line_edit is None:
            return
        upper = text.upper()
        if upper == text:
            return
        cursor = line_edit.cursorPosition()
        line_edit.blockSignals(True)
        line_edit.setText(upper)
        line_edit.setCursorPosition(cursor)
        line_edit.blockSignals(False)

    def _resolve_currency_choice(self) -> str:
        """Map the combo's current state to a 3-letter code. The order
        matters: if the user picked a real dropdown row, ``currentData``
        carries the code; if they free-typed, we parse the first three
        letters of ``currentText`` as the candidate code (the combo
        shows "USD — US Dollar" so a partial match might leave the text
        in that state; using the first three uppercase letters is the
        safe parse). The validator in ``_on_accept`` rejects anything
        that isn't in the ISO list, so this is just a recovery path."""
        data = self._currency_combo.currentData()
        if isinstance(data, str) and len(data) == 3:
            return data.strip().upper()
        text = self._currency_combo.currentText().strip().upper()
        if " " in text:
            text = text.split(" ", 1)[0]
        return text[:3]

    def _on_accept(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required", "Enter an account name.")
            return

        currency = self._resolve_currency_choice()
        if currency not in ISO_4217_CODES:
            QMessageBox.warning(
                self, "Unknown currency",
                f"{currency or '(empty)'} isn't an active ISO 4217 currency "
                f"code. Pick one from the dropdown — typing a code (USD, "
                f"GBP, EUR…) or part of the currency name narrows the list.",
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
