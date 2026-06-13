"""Create / edit a pay-down (or savings) goal (ADR-058 R4b).

Add mode shows an account picker (the caller passes only eligible accounts);
edit mode pins the account and adds a Delete verb. The target is entered as a
positive **magnitude** — "still owing" for a liability, "balance" for a savings
account — and converted to a signed balance for storage, so the user never
types a negative number. ``kind`` follows the account: a liability is a
``paydown``, an asset a ``savings`` goal.

The dialog is pure UI over values; persistence (``add_budget_goal`` /
``update_budget_goal`` / ``delete_budget_goal``) is the window's job. Created
with ``parent=None`` like the other budget-window dialogs (macOS cascade-close
avoidance, ADR-058 R1).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from mfl_desktop.db.repository import AccountSummary, BudgetGoal


class GoalDialog(QDialog):
    def __init__(
        self,
        *,
        accounts: list[AccountSummary],
        goal: Optional[BudgetGoal] = None,
        account: Optional[AccountSummary] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._goal = goal
        self._deleted = False
        editing = goal is not None
        self.setWindowTitle("Edit goal" if editing else "New goal")
        self.setModal(True)

        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)

        # ── account ──
        self._account_combo: Optional[QComboBox] = None
        if editing:
            assert account is not None
            self._account = account
            form.addRow(
                "Account:",
                QLabel(f"{account.name}  ·  {account.currency}"),
            )
        else:
            self._account_combo = QComboBox()
            for a in accounts:
                self._account_combo.addItem(f"{a.name}  ·  {a.currency}", a)
            self._account = accounts[0]
            self._account_combo.currentIndexChanged.connect(
                self._on_account_changed
            )
            form.addRow("Account:", self._account_combo)

        # ── target magnitude ──
        self._target = QDoubleSpinBox()
        self._target.setRange(0.0, 99_999_999.0)
        self._target.setDecimals(2)
        self._target.setGroupSeparatorShown(True)
        self._target_caption = QLabel()
        form.addRow(self._target_caption, self._target)

        # ── target date ──
        self._date = QDateEdit()
        self._date.setCalendarPopup(True)
        self._date.setDisplayFormat("d MMM yyyy")
        if editing:
            y, m, d = (int(x) for x in goal.target_date.split("-"))
            self._date.setDate(QDate(y, m, d))
        else:
            self._date.setDate(QDate.currentDate().addMonths(12))
        form.addRow("Target date:", self._date)

        # Seed the magnitude + captions from the (initial) account.
        if editing:
            mag = abs(goal.target_amount)
            self._target.setValue(float(mag))
        self._apply_account_framing()

        # ── buttons ──
        buttons = QDialogButtonBox()
        ok = buttons.addButton(
            "Save" if editing else "Create", QDialogButtonBox.AcceptRole
        )
        ok.clicked.connect(self.accept)
        if editing:
            delete = buttons.addButton("Delete", QDialogButtonBox.DestructiveRole)
            delete.clicked.connect(self._on_delete)
        buttons.addButton(QDialogButtonBox.Cancel).clicked.connect(self.reject)
        root.addWidget(buttons)

    # ── framing ──

    def _is_liability(self) -> bool:
        return bool(self._account and self._account.is_liability)

    def _apply_account_framing(self) -> None:
        """Update the target caption + currency prefix for the chosen account.
        A liability pays *down* toward a balance still owed (0 = cleared); an
        asset saves *up* toward a balance."""
        ccy = self._account.currency if self._account else ""
        self._target.setPrefix(f"{ccy} " if ccy else "")
        if self._is_liability():
            self._target_caption.setText("Pay down until owing:")
            self._target.setToolTip(
                "The balance you want to still owe by the target date. "
                "0 = fully paid off."
            )
        else:
            self._target_caption.setText("Save up to:")
            self._target.setToolTip("The balance you want to reach.")

    def _on_account_changed(self, _idx: int) -> None:
        if self._account_combo is not None:
            self._account = self._account_combo.currentData()
        self._apply_account_framing()

    def _on_delete(self) -> None:
        self._deleted = True
        self.accept()

    # ── results ──

    def was_deleted(self) -> bool:
        return self._deleted

    def result_account_id(self) -> int:
        return self._account.id

    def result_kind(self) -> str:
        return "paydown" if self._is_liability() else "savings"

    def result_target_amount(self) -> Decimal:
        """Signed target balance: a magnitude entered for a liability is stored
        negative (a debt), an asset's positive."""
        mag = Decimal(str(self._target.value()))
        return -mag if self._is_liability() else mag

    def result_target_date(self) -> str:
        return self._date.date().toString("yyyy-MM-dd")
