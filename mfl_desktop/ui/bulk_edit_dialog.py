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
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from mfl_desktop import txn_status
from mfl_desktop.db.repository import CategoryChoice
from mfl_desktop.ui.category_picker import (
    make_category_picker,
    selected_category_id,
)

# id of the seeded Uncategorised row — used as the default category so the
# user has to deliberately pick a meaningful one before applying.
UNCATEGORISED_ID = 1


class BulkEditDialog(QDialog):
    def __init__(
        self,
        categories: list[CategoryChoice],
        selection_count: int,
        payee_names: Optional[list[str]] = None,
        security_context: Optional[tuple[int, str, str]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Bulk Edit — {selection_count} transactions")
        self.setModal(True)
        self._categories = categories
        self._values: Optional[dict] = None
        # ADR-048: when every selected row is the same investment security,
        # offer a Symbol field that re-tickers that security. (id, name,
        # current_symbol); None for cash selections or a mixed-security set.
        self._security_context = security_context

        # ── field widgets ──

        self._payee_check = QCheckBox("Payee:")
        self._payee_edit = QLineEdit()
        self._payee_edit.setEnabled(False)
        self._payee_edit.setPlaceholderText("(leave empty to clear)")
        self._payee_check.toggled.connect(self._payee_edit.setEnabled)

        # Contains-match, case-insensitive completer over existing payees —
        # same config the register's PayeeTypeaheadDelegate uses (ADR-022).
        # The dialog isn't long-lived so a snapshot at open-time is fine.
        if payee_names:
            completer = QCompleter(payee_names, self._payee_edit)
            completer.setCompletionMode(QCompleter.PopupCompletion)
            completer.setFilterMode(Qt.MatchContains)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setMaxVisibleItems(5)
            self._payee_edit.setCompleter(completer)
            # Explicit popup size so the 5 matches actually fit — Qt's
            # default sizing collapses smaller than expected on Windows
            # when the parent line-edit sits in a modal dialog.
            popup = completer.popup()
            popup.setMinimumWidth(280)
            popup.setMinimumHeight(150)

        self._category_check = QCheckBox("Category:")
        self._category_combo = make_category_picker(
            categories, default_id=UNCATEGORISED_ID,
        )
        self._category_combo.setEnabled(False)
        self._category_check.toggled.connect(self._category_combo.setEnabled)

        self._status_check = QCheckBox("Status:")
        self._status_combo = QComboBox()
        for s in txn_status.STATUSES:
            self._status_combo.addItem(txn_status.label(s), userData=s)
        # Most common bulk-status use case is confirming imports as matched.
        self._set_combo_default(self._status_combo, txn_status.MATCHED)
        self._status_combo.setEnabled(False)
        self._status_check.toggled.connect(self._status_combo.setEnabled)

        self._memo_check = QCheckBox("Memo:")
        self._memo_edit = QLineEdit()
        self._memo_edit.setEnabled(False)
        self._memo_edit.setPlaceholderText("(leave empty to clear)")
        self._memo_check.toggled.connect(self._memo_edit.setEnabled)

        # Investment-only Symbol row (ADR-048). Edits the security master, not
        # the transactions — so it's only offered when the whole selection is
        # one security.
        self._symbol_check: Optional[QCheckBox] = None
        self._symbol_edit: Optional[QLineEdit] = None
        if self._security_context is not None:
            _sid, sec_name, current_symbol = self._security_context
            self._symbol_check = QCheckBox("Symbol:")
            self._symbol_edit = QLineEdit()
            self._symbol_edit.setEnabled(False)
            self._symbol_edit.setText(current_symbol or "")
            self._symbol_edit.setPlaceholderText("ticker, e.g. TSLA (blank = clear)")
            self._symbol_check.toggled.connect(self._symbol_edit.setEnabled)

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
        if self._symbol_check is not None:
            grid.addWidget(self._symbol_check, 4, 0)
            grid.addWidget(self._symbol_edit,  4, 1)
        grid.setColumnStretch(1, 1)

        hint_text = (
            "Tick the fields you want to change. Empty Payee or Memo clears "
            "that field on every selected transaction."
        )
        if self._security_context is not None:
            hint_text += (
                f"  Symbol sets the ticker on “{self._security_context[1]}” "
                "itself — it applies to every transaction of that security."
            )
        hint = QLabel(hint_text)
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
            self._symbol_check is not None and self._symbol_check.isChecked(),
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
            result["status"] = self._status_combo.currentData()
        if self._memo_check.isChecked():
            result["memo"] = self._memo_edit.text()
        if self._symbol_check is not None and self._symbol_check.isChecked():
            # Not a txn field — the handler pops this and routes it to
            # Repository.update_security for the selection's security.
            result["symbol"] = self._symbol_edit.text().strip()
        self._values = result
        self.accept()

    def values(self) -> Optional[dict]:
        """Returns the apply-ready kwargs dict, or None if the dialog was
        cancelled."""
        return self._values
