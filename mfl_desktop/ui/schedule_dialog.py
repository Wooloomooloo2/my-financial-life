"""Modal dialog for creating or editing a scheduled transaction.

Round A of the budget arc (ADR-023). The dialog collects the fields
``Repository.create_scheduled_txn`` / ``update_scheduled_txn`` need, plus
cadence + anchor + optional end date + auto-post + variable flags. A
transfer-kind category reveals an inline destination-account picker
since a schedule's destination is part of the template (no post-time
prompt like the New Transaction dialog has).

The dialog is purely value-producing: accept → caller reads ``values()``,
cancel → returns None. Matches the shape of NewTransactionDialog and
BulkEditDialog.

ADR-027 adds the ``seed`` constructor parameter — a ``ScheduleSeed``
dataclass carrying pre-fill values when the dialog is opened from an
existing object (today: an existing transaction via right-click in the
register). The dialog stays in create mode; seed values just fill the
form so the user can confirm-and-save.
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
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import (
    AccountSummary,
    CategoryChoice,
    SCHEDULE_CADENCES,
    ScheduledTxnRow,
)
from mfl_desktop.ui.category_picker import (
    make_category_picker,
    selected_category_id,
)


_CADENCE_LABELS = {
    "weekly":    "Weekly",
    "biweekly":  "Bi-weekly",
    "monthly":   "Monthly",
    "quarterly": "Quarterly",
    "annual":    "Annually",
}


@dataclass(frozen=True)
class ScheduleSeed:
    """Pre-fill values for a New Schedule dialog opened from another
    object (ADR-027).

    All fields are optional — the dialog applies whichever are provided
    and leaves the rest at their defaults. Amount is signed (negative =
    outflow), matching the rest of the codebase's sign convention.
    """
    account_id: Optional[int] = None
    payee_name: Optional[str] = None
    category_id: Optional[int] = None
    transfer_to_account_id: Optional[int] = None
    amount: Optional[Decimal] = None
    anchor_date: Optional[str] = None
    cadence: Optional[str] = None
    memo: Optional[str] = None


@dataclass(frozen=True)
class ScheduleDialogValues:
    """Validated form values ready to hand to the Repository."""
    account_id: int
    payee_name: str
    category_id: int
    transfer_to_account_id: Optional[int]
    estimated_amount: Decimal      # signed; negative = outflow
    variable: bool
    memo: str
    cadence: str
    anchor_date: str               # ISO 'YYYY-MM-DD'
    next_due_date: str             # ISO; defaults to anchor in create mode
    end_date: Optional[str]        # ISO or None
    auto_post: bool
    notes: str


class ScheduleDialog(QDialog):
    def __init__(
        self,
        accounts: list[AccountSummary],
        categories: list[CategoryChoice],
        default_account_id: Optional[int] = None,
        existing: Optional[ScheduledTxnRow] = None,
        seed: Optional[ScheduleSeed] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._accounts = accounts
        self._categories = categories
        self._existing = existing

        is_edit = existing is not None
        self.setWindowTitle("Edit Schedule" if is_edit else "New Schedule")
        self.setModal(True)

        # Resolve initial values: existing (edit mode) takes precedence over
        # seed (create-from-txn) which takes precedence over hard defaults.
        seed = seed if not is_edit else None  # seed irrelevant in edit mode

        # ── widgets ──

        initial_account_id = (
            existing.account_id if is_edit
            else (
                (seed.account_id if seed else None)
                or default_account_id
                or (accounts[0].id if accounts else None)
            )
        )
        self._account_combo = self._build_account_combo(
            accounts, default_id=initial_account_id,
        )

        self._payee_edit = QLineEdit()
        self._payee_edit.setPlaceholderText("Optional")
        initial_payee = (
            existing.payee_name if is_edit
            else (seed.payee_name if seed else "")
        )
        if initial_payee:
            self._payee_edit.setText(initial_payee)

        # Searchable category combo — same shape as the New Transaction dialog.
        # Default to whatever the existing schedule used, or seed-provided,
        # or Uncategorised on create (id=1).
        default_category_id = (
            existing.category_id if is_edit
            else ((seed.category_id if seed else None) or 1)
        )
        self._category_combo = make_category_picker(
            categories, default_id=default_category_id,
        )
        self._category_combo.currentIndexChanged.connect(
            self._on_category_changed
        )
        self._category_combo.editTextChanged.connect(
            self._on_category_changed
        )

        # Transfer destination — same combo shape as the source account picker.
        # Visibility is toggled by _on_category_changed when the picked
        # category's kind is 'transfer'.
        self._transfer_label = QLabel("Transfer to:")
        initial_transfer_to = (
            existing.transfer_to_account_id if is_edit
            else (seed.transfer_to_account_id if seed else None)
        )
        self._transfer_to_combo = self._build_account_combo(
            accounts, default_id=initial_transfer_to,
        )

        # Direction + amount: direction encoded in sign, amount entered positive.
        initial_amount = (
            existing.estimated_amount if is_edit
            else (seed.amount if seed else None)
        )
        self._direction_out = QRadioButton("Money out")
        self._direction_in = QRadioButton("Money in")
        if initial_amount is not None and initial_amount > 0:
            self._direction_in.setChecked(True)
        else:
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
        if initial_amount is not None:
            self._amount_edit.setText(f"{abs(initial_amount):.2f}")

        self._variable_check = QCheckBox(
            "Variable amount — prompt for actual at post time"
        )
        if is_edit:
            self._variable_check.setChecked(existing.variable)

        # Cadence dropdown.
        self._cadence_combo = QComboBox()
        for key in SCHEDULE_CADENCES:
            self._cadence_combo.addItem(_CADENCE_LABELS[key], userData=key)
        default_cadence = (
            existing.cadence if is_edit
            else ((seed.cadence if seed else None) or "monthly")
        )
        for i in range(self._cadence_combo.count()):
            if self._cadence_combo.itemData(i) == default_cadence:
                self._cadence_combo.setCurrentIndex(i)
                break

        # Anchor date — first occurrence's posting date.
        self._anchor_edit = QDateEdit()
        self._anchor_edit.setDisplayFormat("yyyy-MM-dd")
        self._anchor_edit.setCalendarPopup(True)
        if is_edit:
            self._anchor_edit.setDate(_qdate(existing.anchor_date))
        elif seed and seed.anchor_date:
            self._anchor_edit.setDate(_qdate(seed.anchor_date))
        else:
            self._anchor_edit.setDate(QDate.currentDate())

        # Next-due date — only meaningful when editing an existing schedule
        # (lets the user skip/replay). On create, hidden; the repository
        # defaults next_due_date to anchor_date.
        self._next_due_edit = QDateEdit()
        self._next_due_edit.setDisplayFormat("yyyy-MM-dd")
        self._next_due_edit.setCalendarPopup(True)
        if is_edit:
            self._next_due_edit.setDate(_qdate(existing.next_due_date))

        # End date — optional cap. A "(no end date)" checkbox disables the
        # picker; matches how Qt apps typically expose optional date fields.
        self._end_check = QCheckBox("Stop after end date:")
        self._end_edit = QDateEdit()
        self._end_edit.setDisplayFormat("yyyy-MM-dd")
        self._end_edit.setCalendarPopup(True)
        self._end_edit.setEnabled(False)
        if is_edit and existing.end_date:
            self._end_check.setChecked(True)
            self._end_edit.setEnabled(True)
            self._end_edit.setDate(_qdate(existing.end_date))
        else:
            self._end_edit.setDate(QDate.currentDate().addYears(1))
        self._end_check.toggled.connect(self._end_edit.setEnabled)
        end_row = QHBoxLayout()
        end_row.setContentsMargins(0, 0, 0, 0)
        end_row.addWidget(self._end_check)
        end_row.addWidget(self._end_edit, stretch=1)

        self._auto_post_check = QCheckBox(
            "Auto-post on its due date when MFL is launched"
        )
        if is_edit:
            self._auto_post_check.setChecked(existing.auto_post)

        self._memo_edit = QLineEdit()
        self._memo_edit.setPlaceholderText("Optional")
        initial_memo = (
            existing.memo if is_edit
            else (seed.memo if seed else "")
        )
        if initial_memo:
            self._memo_edit.setText(initial_memo)

        self._notes_edit = QPlainTextEdit()
        self._notes_edit.setPlaceholderText("Optional — private notes about this schedule")
        self._notes_edit.setFixedHeight(60)
        if is_edit and existing.notes:
            self._notes_edit.setPlainText(existing.notes)

        # ── layout ──

        form = QFormLayout()
        form.addRow("Account:", self._account_combo)
        form.addRow("Payee:", self._payee_edit)
        form.addRow("Category:", self._category_combo)
        form.addRow(self._transfer_label, self._transfer_to_combo)
        form.addRow("Direction:", _wrap(direction_row))
        form.addRow("Estimated amount:", self._amount_edit)
        form.addRow("", self._variable_check)
        form.addRow("Cadence:", self._cadence_combo)
        form.addRow("Next occurrence:", self._anchor_edit)
        if is_edit:
            form.addRow("Next due date:", self._next_due_edit)
        form.addRow("End date:", _wrap(end_row))
        form.addRow("", self._auto_post_check)
        form.addRow("Memo:", self._memo_edit)
        form.addRow("Notes:", self._notes_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.resize(460, self.sizeHint().height())

        # Initial transfer-row visibility based on whichever category is
        # currently picked (covers create-with-seed, create-empty, edit).
        self._on_category_changed()

        self._values: Optional[ScheduleDialogValues] = None

    # ── helpers ──

    def _build_account_combo(
        self,
        accounts: list[AccountSummary],
        default_id: Optional[int],
    ) -> QComboBox:
        combo = QComboBox()
        for acct in accounts:
            combo.addItem(
                f"{acct.name}  ·  {acct.currency}", userData=acct.id,
            )
        if default_id is not None:
            for i in range(combo.count()):
                if combo.itemData(i) == default_id:
                    combo.setCurrentIndex(i)
                    break
        return combo

    def _on_category_changed(self, *_args) -> None:
        """Reveal/hide the Transfer-to row based on the picked category's
        kind. Called both when the combo's index changes and when the user
        is mid-typing in the editable combo (so the field re-hides if they
        type their way away from a transfer-kind match)."""
        kind = self._picked_category_kind()
        show = kind == "transfer"
        self._transfer_label.setVisible(show)
        self._transfer_to_combo.setVisible(show)

    def _picked_category_kind(self) -> Optional[str]:
        """Look up the kind of the currently-picked category by id.
        Returns None if the typed text doesn't match a real category."""
        cid = selected_category_id(self._category_combo)
        if cid is None:
            return None
        for c in self._categories:
            if c.id == cid:
                return c.kind
        return None

    def _on_accept(self) -> None:
        account_id = self._account_combo.currentData()
        if account_id is None:
            QMessageBox.warning(self, "Account required", "Pick an account.")
            return

        category_id = selected_category_id(self._category_combo)
        if category_id is None:
            QMessageBox.warning(
                self, "Category required",
                "Pick a category from the list.",
            )
            return
        kind = self._picked_category_kind()

        transfer_to_id: Optional[int] = None
        if kind == "transfer":
            transfer_to_id = self._transfer_to_combo.currentData()
            if transfer_to_id is None:
                QMessageBox.warning(
                    self, "Destination required",
                    "Transfer categories need a destination account.",
                )
                return
            if transfer_to_id == account_id:
                QMessageBox.warning(
                    self, "Destination must differ",
                    "The destination account can't be the same as the source.",
                )
                return

        raw = self._amount_edit.text().strip().replace(",", "")
        if not raw:
            QMessageBox.warning(
                self, "Amount required", "Enter an estimated amount.",
            )
            return
        try:
            magnitude = Decimal(raw)
        except InvalidOperation:
            QMessageBox.warning(
                self, "Invalid amount", f"Could not parse {raw!r}.",
            )
            return
        if magnitude <= 0:
            QMessageBox.warning(
                self, "Invalid amount",
                "Amount must be greater than zero — use Direction to choose "
                "money out vs money in.",
            )
            return
        amount = -magnitude if self._direction_out.isChecked() else magnitude

        cadence = self._cadence_combo.currentData()
        anchor_date = _iso(self._anchor_edit.date())

        if self._existing is not None:
            next_due_date = _iso(self._next_due_edit.date())
        else:
            next_due_date = anchor_date

        end_date: Optional[str]
        if self._end_check.isChecked():
            end_date = _iso(self._end_edit.date())
            if end_date < anchor_date:
                QMessageBox.warning(
                    self, "Invalid end date",
                    "End date can't be before the first occurrence.",
                )
                return
        else:
            end_date = None

        self._values = ScheduleDialogValues(
            account_id=int(account_id),
            payee_name=self._payee_edit.text().strip(),
            category_id=int(category_id),
            transfer_to_account_id=(
                int(transfer_to_id) if transfer_to_id is not None else None
            ),
            estimated_amount=amount,
            variable=self._variable_check.isChecked(),
            memo=self._memo_edit.text().strip(),
            cadence=cadence,
            anchor_date=anchor_date,
            next_due_date=next_due_date,
            end_date=end_date,
            auto_post=self._auto_post_check.isChecked(),
            notes=self._notes_edit.toPlainText().strip(),
        )
        self.accept()

    def values(self) -> Optional[ScheduleDialogValues]:
        return self._values


def _qdate(iso: str) -> QDate:
    d = date.fromisoformat(iso)
    return QDate(d.year, d.month, d.day)


def _iso(qd: QDate) -> str:
    return date(qd.year(), qd.month(), qd.day()).isoformat()


def _wrap(inner_layout) -> QWidget:
    """Wrap a layout so QFormLayout.addRow accepts it as a field widget."""
    w = QWidget()
    w.setLayout(inner_layout)
    return w
