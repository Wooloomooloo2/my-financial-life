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
from typing import Callable, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from mfl_desktop import txn_status
from mfl_desktop.db.repository import AccountSummary, CategoryChoice
from mfl_desktop.ui.date_widgets import make_date_edit
from mfl_desktop.ui.category_picker import (
    make_category_picker,
    selected_category_id,
)


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
        payee_category_lookup: Optional[Callable[[str], Optional[int]]] = None,
        payee_names: Optional[list[str]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Transaction")
        self.setModal(True)
        self._accounts = accounts
        self._categories = categories
        # ADR-073: optional name → remembered-category lookup. When set, the
        # category is pre-filled after the payee is entered (only if the user
        # hasn't already chosen a non-Uncategorised category).
        self._payee_category_lookup = payee_category_lookup

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

        self._date_edit = make_date_edit()

        self._payee_edit = QLineEdit()
        self._payee_edit.setPlaceholderText("Optional")
        self._payee_edit.editingFinished.connect(self._maybe_prefill_category)
        # Payee autocomplete — same contains-match, case-insensitive completer
        # the register's PayeeTypeaheadDelegate and BulkEditDialog use, so the
        # three entry surfaces behave identically (ADR-105). Names are a
        # snapshot taken when the dialog opens (canonical payees, ADR-028).
        if payee_names:
            completer = QCompleter(payee_names, self._payee_edit)
            completer.setCompletionMode(QCompleter.PopupCompletion)
            completer.setFilterMode(Qt.MatchContains)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setMaxVisibleItems(8)
            self._payee_edit.setCompleter(completer)
            # Picking a completion fills the field but doesn't fire
            # editingFinished, so pre-fill the remembered category then too.
            completer.activated.connect(
                lambda _text: self._maybe_prefill_category()
            )

        # Searchable dropdown — same helper used in BulkEditDialog so the
        # two surfaces behave identically. Default to Uncategorised (id=1).
        self._category_combo = make_category_picker(categories, default_id=1)

        self._status_combo = QComboBox()
        self._status_combo.addItems(txn_status.labels())
        self._status_combo.setCurrentText(txn_status.label(txn_status.PENDING))

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

        # ADR-107: lay the buttons out by hand rather than via
        # QDialogButtonBox. The box's macOS policy pushes ActionRole buttons
        # (Save & New, Split) to the *left*, away from where the eye lands;
        # we want the primary action on the right and consistent across
        # platforms. Order: secondary action left, then Cancel / Save /
        # Save & New on the right, with Save & New as the default (Enter).
        # ADR-051: "Split…" hands the header + amount to the split dialog,
        # which collects the per-category lines. Category isn't required here.
        split_btn = QPushButton("Split…")
        split_btn.clicked.connect(self._on_split)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._on_accept)
        # ADR-105: "Save & New" commits this transaction and immediately
        # reopens a fresh dialog on the same account, for fast multi-entry.
        save_new_btn = QPushButton("Save && New")
        save_new_btn.clicked.connect(self._on_save_and_new)
        # Save & New is the default — Enter commits and reopens for fast
        # multi-entry. Pin autoDefault off on the others so a focused button
        # can't quietly become the default.
        for b in (split_btn, cancel_btn, save_btn):
            b.setAutoDefault(False)
        save_new_btn.setDefault(True)
        save_new_btn.setAutoDefault(True)

        button_row = QHBoxLayout()
        button_row.addWidget(split_btn)
        button_row.addStretch(1)
        button_row.addWidget(cancel_btn)
        button_row.addWidget(save_btn)
        button_row.addWidget(save_new_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(button_row)

        self.resize(420, self.sizeHint().height())
        self._values: Optional[NewTransactionValues] = None
        self._split_requested = False
        self._save_and_new_requested = False

    # ── helpers ──

    @staticmethod
    def _wrap(inner_layout) -> "QHBoxLayout":
        """Wrap a layout so QFormLayout.addRow accepts it as a field widget."""
        from PySide6.QtWidgets import QWidget
        w = QWidget()
        w.setLayout(inner_layout)
        return w

    def _maybe_prefill_category(self) -> None:
        """ADR-073: pre-fill the category from the payee's remembered
        auto-category, but only when the user hasn't already picked a real
        (non-Uncategorised) category, so a deliberate choice is never clobbered."""
        if self._payee_category_lookup is None:
            return
        name = self._payee_edit.text().strip()
        if not name:
            return
        current = selected_category_id(self._category_combo)
        if current is not None and current != 1:
            return
        cat_id = self._payee_category_lookup(name)
        if cat_id is None:
            return
        for i in range(self._category_combo.count()):
            if self._category_combo.itemData(i) == cat_id:
                self._category_combo.setCurrentIndex(i)
                break

    def _on_save_and_new(self) -> None:
        """ADR-105: validate + accept like Save, but flag the caller to
        reopen a fresh dialog afterwards. Reuses the same validation path so
        the two buttons never diverge."""
        self._save_and_new_requested = True
        self._on_accept()
        # If validation failed, _on_accept didn't accept() — clear the flag
        # so a subsequent plain Save isn't mistaken for Save & New.
        if self.result() != QDialog.Accepted:
            self._save_and_new_requested = False

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
            status=txn_status.key_for_label(self._status_combo.currentText()),
            memo=self._memo_edit.text().strip(),
        )
        self.accept()

    def _on_split(self) -> None:
        """ADR-051: validate the header + amount (category not required) and
        accept with the split flag set. The caller opens the split dialog
        seeded with these values; the entered amount becomes the split total."""
        account_id = self._account_combo.currentData()
        if account_id is None:
            QMessageBox.warning(self, "Account required", "Pick an account.")
            return
        raw = self._amount_edit.text().strip().replace(",", "")
        if not raw:
            QMessageBox.warning(
                self, "Amount required",
                "Enter the transaction's total amount, then split it across "
                "categories.",
            )
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
        qd: QDate = self._date_edit.date()
        posted_date = date(qd.year(), qd.month(), qd.day()).isoformat()
        self._values = NewTransactionValues(
            account_id=int(account_id),
            posted_date=posted_date,
            amount=amount,
            payee_name=self._payee_edit.text().strip(),
            category_id=1,                 # placeholder — the lines carry categories
            status=txn_status.key_for_label(self._status_combo.currentText()),
            memo=self._memo_edit.text().strip(),
        )
        self._split_requested = True
        self.accept()

    def values(self) -> Optional[NewTransactionValues]:
        """Return the validated values, or None if the dialog was cancelled."""
        return self._values

    def split_requested(self) -> bool:
        """True when the user clicked Split… rather than Save (ADR-051)."""
        return self._split_requested

    def save_and_new_requested(self) -> bool:
        """True when the user clicked Save & New, so the caller should reopen
        a fresh dialog on the same account after committing (ADR-105)."""
        return self._save_and_new_requested
