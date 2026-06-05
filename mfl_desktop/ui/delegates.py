"""Combo-box delegates for the register's editable columns.

Both delegates commit on `activated` to suppress the "editor does not belong
to this view" warning Qt emits when a combo loses focus after a selection
has already been committed.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QStyledItemDelegate

from mfl_desktop.db.repository import CategoryChoice
from mfl_desktop.ui.register_model import ID_ROLE


class CategoryDelegate(QStyledItemDelegate):
    """Combo populated from a CategoryChoice list. The display label includes
    the immediate parent name in parentheses when present, so sibling-name
    collisions across different branches are visually distinguishable
    (e.g. 'Groceries (Food)' vs 'Groceries (Expense)')."""

    def __init__(self, choices: list[CategoryChoice], parent=None) -> None:
        super().__init__(parent)
        self._choices = choices

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        for choice in self._choices:
            label = (f"{choice.name} ({choice.parent_name})"
                     if choice.parent_name else choice.name)
            combo.addItem(label, userData=choice.id)
        combo.activated.connect(lambda _: self._commit_and_close(combo))
        return combo

    def _commit_and_close(self, editor: QComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QComboBox, index):
        current_id = index.data(ID_ROLE)
        if current_id is None:
            return
        editor.blockSignals(True)
        for i in range(editor.count()):
            if editor.itemData(i) == current_id:
                editor.setCurrentIndex(i)
                break
        editor.blockSignals(False)
        editor.showPopup()

    def setModelData(self, editor: QComboBox, model, index):
        category_id = editor.currentData()
        if category_id is None:
            return
        model.setData(index, category_id, Qt.EditRole)


class StatusDelegate(QStyledItemDelegate):
    """Combo over the four status enum values."""

    STATUSES = ("Pending", "Uncleared", "Cleared", "Reconciled")

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.addItems(self.STATUSES)
        combo.activated.connect(lambda _: self._commit_and_close(combo))
        return combo

    def _commit_and_close(self, editor: QComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QComboBox, index):
        current = index.data(Qt.EditRole) or ""
        editor.blockSignals(True)
        i = editor.findText(current)
        if i >= 0:
            editor.setCurrentIndex(i)
        editor.blockSignals(False)
        editor.showPopup()

    def setModelData(self, editor: QComboBox, model, index):
        model.setData(index, editor.currentText(), Qt.EditRole)
