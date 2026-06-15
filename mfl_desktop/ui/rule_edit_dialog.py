"""Create / edit one auto-categorisation rule (ADR-073).

A rule matches a transaction's raw payee text or memo with one of four
matcher kinds (contains / starts with / ends with / is exactly) and sets a
payee and/or a category at import. The dialog validates that the pattern is
non-empty, that any typed payee resolves to an existing canonical, and that
at least one of payee/category is set. ``values()`` returns the field dict
(or None on cancel) — the caller does the Repository write so it can offer
the retroactive-apply prompt.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from mfl_desktop.db.repository import CategoryChoice, Repository, RuleRow
from mfl_desktop.rules_engine import MATCH_FIELDS, MATCHER_KINDS
from mfl_desktop.ui.category_picker import make_category_picker, selected_category_id
from mfl_desktop.ui import tokens

_NO_PAYEE = "— none —"
_NO_CATEGORY = "— none —"


class RuleEditDialog(QDialog):
    def __init__(
        self,
        repo: Repository,
        categories: list[CategoryChoice],
        rule: Optional[RuleRow] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Edit Rule" if rule else "New Rule")
        self.setModal(True)

        self._field_combo = QComboBox()
        for key, label in MATCH_FIELDS.items():
            self._field_combo.addItem(label, key)

        self._kind_combo = QComboBox()
        for key, label in MATCHER_KINDS.items():
            self._kind_combo.addItem(label, key)

        self._pattern_edit = QLineEdit()
        self._pattern_edit.setPlaceholderText("e.g. TESCO")

        # Payee target — pick from existing canonicals (editable for search);
        # a leading "none" sentinel means "don't set a payee".
        self._payee_combo = QComboBox()
        self._payee_combo.setEditable(True)
        self._payee_combo.setInsertPolicy(QComboBox.NoInsert)
        self._payee_combo.addItem(_NO_PAYEE, None)
        for cid, name in repo.list_canonical_payees():
            self._payee_combo.addItem(name, cid)
        _configure_typeahead(self._payee_combo)

        # Category target — same searchable picker as everywhere, with a
        # leading "none" sentinel.
        self._category_combo = make_category_picker(categories)
        self._category_combo.insertItem(0, _NO_CATEGORY, None)
        self._category_combo.setCurrentIndex(0)

        self._priority = QSpinBox()
        self._priority.setRange(1, 999)
        self._priority.setValue(100)
        self._priority.setToolTip(
            "Lower numbers win. When several rules match a transaction, the "
            "lowest-priority rule sets each field first."
        )

        if rule is not None:
            self._preload(rule)

        form = QFormLayout()
        form.addRow("Match:", self._field_combo)
        form.addRow("That:", self._kind_combo)
        form.addRow("Text:", self._pattern_edit)
        form.addRow("Set payee:", self._payee_combo)
        form.addRow("Set category:", self._category_combo)
        form.addRow("Priority:", self._priority)

        hint = QLabel(
            "Rules run when transactions are imported. Set a payee, a "
            "category, or both."
        )
        hint.setWordWrap(True)
        tokens.themed(hint, "color: {muted};")  # slate-500

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)
        self.resize(440, self.sizeHint().height())

        self._values: Optional[dict] = None

    def _preload(self, rule: RuleRow) -> None:
        self._select_data(self._field_combo, rule.match_field)
        self._select_data(self._kind_combo, rule.pattern_kind)
        self._pattern_edit.setText(rule.pattern)
        if rule.set_payee_id is not None:
            self._select_data(self._payee_combo, rule.set_payee_id)
        if rule.set_category_id is not None:
            for i in range(self._category_combo.count()):
                if self._category_combo.itemData(i) == rule.set_category_id:
                    self._category_combo.setCurrentIndex(i)
                    break
        self._priority.setValue(rule.priority)

    @staticmethod
    def _select_data(combo: QComboBox, data) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                return

    def _resolve_payee(self) -> tuple[bool, Optional[int]]:
        """Return (ok, payee_id). ok=False means the typed text didn't match
        an existing canonical (caller should not proceed)."""
        text = self._payee_combo.currentText().strip()
        if not text or text == _NO_PAYEE:
            return True, None
        for i in range(self._payee_combo.count()):
            if self._payee_combo.itemText(i) == text:
                return True, self._payee_combo.itemData(i)
        return False, None

    def _on_accept(self) -> None:
        pattern = self._pattern_edit.text().strip()
        if not pattern:
            QMessageBox.warning(
                self, "Text required", "Enter the text the rule matches.",
            )
            return
        ok, payee_id = self._resolve_payee()
        if not ok:
            QMessageBox.warning(
                self, "Unknown payee",
                "Pick an existing payee from the list, or clear the field.",
            )
            return
        category_id = selected_category_id(self._category_combo)
        if payee_id is None and category_id is None:
            QMessageBox.warning(
                self, "Nothing to set",
                "A rule must set a payee, a category, or both.",
            )
            return
        self._values = {
            "pattern": pattern,
            "pattern_kind": self._kind_combo.currentData(),
            "match_field": self._field_combo.currentData(),
            "set_payee_id": payee_id,
            "set_category_id": category_id,
            "priority": self._priority.value(),
        }
        self.accept()

    def values(self) -> Optional[dict]:
        return self._values


def _configure_typeahead(combo: QComboBox) -> None:
    completer = combo.completer()
    if completer is None:
        return
    completer.setCompletionMode(QCompleter.PopupCompletion)
    completer.setFilterMode(Qt.MatchContains)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
