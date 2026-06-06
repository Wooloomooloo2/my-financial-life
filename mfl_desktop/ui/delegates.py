"""Editor delegates for the register's editable columns.

Three delegates back the editable cells:

- ``PayeeTypeaheadDelegate`` — free-text ``QLineEdit`` plus a ``QCompleter``
  populated from the existing payee list. New payee names are committed as-is;
  ``Repository.update_transaction_payee`` calls ``get_or_create_payee`` so
  unrecognised text becomes a new payee silently (matches the v0.1
  free-text-payee behaviour, see ADR-012).

- ``CategoryTypeaheadDelegate`` — editable ``QComboBox`` built by the same
  ``make_category_picker`` helper as the dialog combos, so the in-cell
  experience matches the New Transaction and Bulk Edit dialogs exactly
  (contains-match popup, case-insensitive). When the user types a name that
  doesn't resolve to an existing category, the delegate calls back into the
  window for a confirm-and-create flow — see ADR-022 for the policy.

- ``StatusDelegate`` — small fixed combo over the four transaction statuses.
  Unchanged from the v1 delegate.

All combo-shaped delegates commit on ``activated`` to suppress the "editor
does not belong to this view" warning Qt emits when a combo loses focus
after a selection has already been committed.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QLineEdit,
    QStyledItemDelegate,
)

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.category_picker import (
    make_category_picker,
    selected_category_id,
)
from mfl_desktop.ui.register_model import ID_ROLE


class PayeeTypeaheadDelegate(QStyledItemDelegate):
    """QLineEdit with a contains-match completer over existing payees.

    Names are read fresh from the repository on each ``createEditor`` call —
    cheap (single ``SELECT name FROM payee``) and avoids forcing the window
    to keep a cached payee list in sync with every inline edit.
    """

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        names = self._repo.list_payee_names()
        completer = QCompleter(names, editor)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setMaxVisibleItems(5)
        editor.setCompleter(completer)
        # Give the popup enough horizontal room to read full payee names but
        # let Qt size the height to the actual number of matches (capped at
        # maxVisibleItems above). A fixed minimum height made the popup
        # hang ~5 rows down over the register even when only one match was
        # showing, which looked oppressive.
        popup = completer.popup()
        popup.setMinimumWidth(280)
        return editor

    def setEditorData(self, editor: QLineEdit, index) -> None:
        current = index.data(Qt.EditRole) or ""
        editor.setText(str(current))
        editor.selectAll()

    def setModelData(self, editor: QLineEdit, model, index) -> None:
        model.setData(index, editor.text().strip(), Qt.EditRole)


class CategoryTypeaheadDelegate(QStyledItemDelegate):
    """Editable category combo with inline-create on unknown text.

    The editor is the same searchable combo the dialog flows use, so the
    typeahead behaviour is consistent across the whole app. On commit:

    - If the typed/selected text matches a known category label exactly,
      that category's id is written to the cell.
    - If the text doesn't match any category, the delegate calls
      ``on_create_category(name)``. The window owns the actual create
      logic — it shows a confirm dialog, runs ``Repository.create_category``,
      refreshes the cached category list and the filter combo, and returns
      the new id. The delegate writes that id into the cell.
    - If the user cancels the inline-create confirmation, the callback
      returns ``None`` and the cell is left unchanged (no setData call).

    See ADR-022 for the inline-create policy.
    """

    def __init__(
        self,
        repo: Repository,
        on_create_category: Callable[[str], Optional[int]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._on_create_category = on_create_category

    def createEditor(self, parent, option, index):
        # Fetch fresh on every open so a category created inline in one
        # cell is visible the next time the delegate is asked to edit
        # another cell. Cheap query (single SELECT with a LEFT JOIN) and
        # avoids forcing the window to rebind the delegate after every
        # category mutation.
        choices = self._repo.list_categories_flat()
        combo = make_category_picker(choices)
        combo.setParent(parent)
        # Commit when the user clicks (or Enters) an item from the popup.
        # The free-text path falls through to setModelData on focus-out.
        combo.activated.connect(lambda _: self._commit_and_close(combo))
        return combo

    def _commit_and_close(self, editor: QComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QComboBox, index) -> None:
        current_id = index.data(ID_ROLE)
        editor.blockSignals(True)
        matched = False
        if current_id is not None:
            for i in range(editor.count()):
                if editor.itemData(i) == current_id:
                    editor.setCurrentIndex(i)
                    matched = True
                    break
        if not matched:
            # No prior selection (e.g. brand-new row): leave the line edit
            # empty so the user can start typing immediately. Selecting all
            # would force them to clear placeholder text first.
            editor.setEditText("")
        editor.blockSignals(False)
        # Don't force showPopup() here — the editable combo already has a
        # dropdown arrow for click-pick, and the QCompleter handles the
        # typing path. Stacking the combo popup over the completer popup
        # produces flicker. Just select-all so the user can replace.
        line_edit = editor.lineEdit()
        if line_edit is not None:
            line_edit.selectAll()

    def setModelData(self, editor: QComboBox, model, index) -> None:
        category_id = selected_category_id(editor)
        if category_id is not None:
            model.setData(index, category_id, Qt.EditRole)
            return
        # Unknown text — give the window a chance to create the category
        # via its confirm-and-create callback. Blank input leaves the cell
        # unchanged; the user is plainly just bailing out.
        typed = editor.currentText().strip()
        if not typed:
            return
        new_id = self._on_create_category(typed)
        if new_id is None:
            return
        model.setData(index, new_id, Qt.EditRole)


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
