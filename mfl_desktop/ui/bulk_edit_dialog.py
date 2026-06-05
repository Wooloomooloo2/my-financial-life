"""Modal dialog for editing multiple transactions in one go.

Each editable field (Payee, Category, Status, Memo) has a checkbox in
front of it. Only checked fields are applied to the selection — so a user
can re-categorise 30 rows without touching their payees, or mark a batch
as Cleared without re-typing memos.

Empty payee or memo, with the corresponding checkbox ticked, clears the
field on every selected transaction. Status and category cannot be cleared
(they're NOT NULL in the schema); their checkboxes default off.

Returns a dict of {field_name: value} containing only the checked fields,
ready to be ``**``-expanded into ``Repository.bulk_update_transactions``.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from mfl_desktop.db.repository import CategoryChoice
from mfl_desktop.ui.category_picker import (
    make_category_picker,
    selected_category_id,
)

STATUSES = ("Pending", "Uncleared", "Cleared", "Reconciled")
# id of the seeded Uncategorised row — used as the default category so the
# user has to deliberately pick a meaningful one before applying.
UNCATEGORISED_ID = 1


class BulkEditDialog(QDialog):
    def __init__(
        self,
        categories: list[CategoryChoice],
        selection_count: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Bulk Edit — {selection_count} transactions")
        self.setModal(True)
        self._categories = categories
        self._values: Optional[dict] = None

        # ── field widgets ──

        self._payee_check = QCheckBox("Payee:")
        self._payee_edit = QLineEdit()
        self._payee_edit.setEnabled(False)
        self._payee_edit.setPlaceholderText("(leave empty to clear)")
        self._payee_check.toggled.connect(self._payee_edit.setEnabled)

        self._category_check = QCheckBox("Category:")
        self._category_combo = make_category_picker(
            categories, default_id=UNCATEGORISED_ID,
        )
        self._category_combo.setEnabled(False)
        self._category_check.toggled.connect(self._category_combo.setEnabled)

        self._status_check = QCheckBox("Status:")
        self._status_combo = QComboBox()
        for s in STATUSES:
            self._status_combo.addItem(s, userData=s)
        # Most common bulk-status use case is confirming imports as Cleared.
        self._set_combo_default(self._status_combo, "Cleared")
        self._status_combo.setEnabled(False)
        self._status_check.toggled.connect(self._status_combo.setEnabled)

        self._memo_check = QCheckBox("Memo:")
        self._memo_edit = QLineEdit()
        self._memo_edit.setEnabled(False)
        self._memo_edit.setPlaceholderText("(leave empty to clear)")
        self._memo_check.toggled.connect(self._memo_edit.setEnabled)

        # ── layout ──

        grid = QGridLayout()
        grid.addWidget(self._payee_check,    0, 0)
        grid.addWidget(self._payee_edit,     0, 1)
        grid.addWidget(self._category_check, 1, 0)
        grid.addWidget(self._category_combo, 1, 1)
        grid.addWidget(self._status_check,   2, 0)
        grid.addWidget(self._status_combo,   2, 1)
        grid.addWidget(self._memo_check,     3, 0)
        grid.addWidget(self._memo_edit,      3, 1)
        grid.setColumnStretch(1, 1)

        hint = QLabel(
            "Tick the fields you want to change. Empty Payee or Memo clears "
            "that field on every selected transaction."
        )
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("&Apply")
        buttons.accepted.connect(self._on_apply)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addWidget(hint)
        layout.addWidget(buttons)
        self.resize(460, self.sizeHint().height())

    # ── helpers ──

    @staticmethod
    def _set_combo_default(combo: QComboBox, value) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    def _on_apply(self) -> None:
        any_checked = any([
            self._payee_check.isChecked(),
            self._category_check.isChecked(),
            self._status_check.isChecked(),
            self._memo_check.isChecked(),
        ])
        if not any_checked:
            QMessageBox.warning(
                self, "Nothing to change",
                "Tick at least one field to apply.",
            )
            return

        result: dict = {}
        if self._payee_check.isChecked():
            result["payee_name"] = self._payee_edit.text()
        if self._category_check.isChecked():
            cid = selected_category_id(self._category_combo)
            if cid is None:
                QMessageBox.warning(
                    self, "Category required",
                    "Pick a category from the list — typing a name that "
                    "doesn't match a category isn't enough.",
                )
                return
            result["category_id"] = cid
        if self._status_check.isChecked():
            result["status"] = self._status_combo.currentText()
        if self._memo_check.isChecked():
            result["memo"] = self._memo_edit.text()
        self._values = result
        self.accept()

    def values(self) -> Optional[dict]:
        """Returns the apply-ready kwargs dict, or None if the dialog was
        cancelled."""
        return self._values
