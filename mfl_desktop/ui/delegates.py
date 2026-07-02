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

from PySide6.QtCore import QDate, QEvent, Qt
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QComboBox,
    QCompleter,
    QDateEdit,
    QLineEdit,
    QStyledItemDelegate,
)

from mfl_desktop import txn_status
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
        self._editor = editor
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
        # A4 fix (ADR-022 follow-up, 2026-06-14): inside a QTableView the
        # delegate's editor lives under the view's edit event filter, which
        # commits/closes the editor on focus-out and Enter. Picking a
        # completion (click or Enter in the popup) therefore raced with the
        # view closing the editor on the half-typed text, so inline typeahead
        # "didn't work" even though the bulk-edit dialog (no view filter) did.
        # Commit the chosen completion explicitly — same commit-on-activated
        # pattern the Category/Status combo delegates use. The completer popup
        # is a Qt::Popup, so it receives the Enter/click first and fires
        # activated before the view's filter sees the key.
        completer.activated[str].connect(
            lambda text, e=editor: self._accept_completion(e, text)
        )
        # Watch the completer popup too, so Tab is caught while it's open —
        # the popup, not the line edit, has focus then (see eventFilter).
        popup.installEventFilter(self)
        return editor

    def _accept_completion(self, editor: QLineEdit, text: str) -> None:
        editor.setText(text)
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def eventFilter(self, obj, event):
        """Make Tab / Shift+Tab commit and **advance to the next editable
        cell** (payee → category), opening its editor instead of just closing.

        The view installs this delegate as the editor's event filter; we also
        install it on the completer popup (which holds focus while open). When
        the popup is showing, Tab first accepts the highlighted completion, so
        normalising a payee to an existing one and tabbing on lands a clean
        value in the next field."""
        if (
            event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Tab, Qt.Key_Backtab)
        ):
            editor = obj if isinstance(obj, QLineEdit) else getattr(self, "_editor", None)
            if editor is not None:
                self._take_visible_completion(editor)
                self.commitData.emit(editor)
                hint = (
                    QAbstractItemDelegate.EditNextItem
                    if event.key() == Qt.Key_Tab
                    else QAbstractItemDelegate.EditPreviousItem
                )
                self.closeEditor.emit(editor, hint)
                return True
        # Only the editor's events belong to the base filter; popup events
        # (obj is the completion list view) must not be forwarded to it.
        if isinstance(obj, QLineEdit):
            return super().eventFilter(obj, event)
        return False

    @staticmethod
    def _take_visible_completion(editor: QLineEdit) -> None:
        """If the completer popup is open with a highlighted row, adopt it as
        the editor text before committing — so Tab accepts the suggestion."""
        completer = editor.completer()
        if completer is None:
            return
        popup = completer.popup()
        if popup is not None and popup.isVisible():
            idx = popup.currentIndex()
            if idx.isValid():
                editor.setText(idx.data())
            elif completer.currentCompletion():
                editor.setText(completer.currentCompletion())

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


class DateEditDelegate(QStyledItemDelegate):
    """Calendar-popup editor for the register's Date column.

    Uses a ``QDateEdit`` with the same ``yyyy-MM-dd`` display format and
    calendar popup as the New Transaction dialog, so inline date entry feels
    identical. The cell stores an ISO date string; the editor parses it on open
    and writes it back the same way. A QDateEdit can only ever produce a valid
    date, so no input validation is needed on the way out.
    """

    _FORMAT = "yyyy-MM-dd"

    def createEditor(self, parent, option, index):
        editor = QDateEdit(parent)
        editor.setCalendarPopup(True)
        editor.setDisplayFormat(self._FORMAT)
        return editor

    def setEditorData(self, editor: QDateEdit, index) -> None:
        qd = QDate.fromString(str(index.data(Qt.EditRole) or ""), self._FORMAT)
        if qd.isValid():
            editor.setDate(qd)

    def setModelData(self, editor: QDateEdit, model, index) -> None:
        model.setData(index, editor.date().toString(self._FORMAT), Qt.EditRole)


class StatusDelegate(QStyledItemDelegate):
    """Combo over the status ladder (ADR-130). Shows Title-case labels but
    reads/writes the stored lowercase key via the item's userData."""

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        for key in txn_status.STATUSES:
            combo.addItem(txn_status.label(key), key)
        combo.activated.connect(lambda _: self._commit_and_close(combo))
        return combo

    def _commit_and_close(self, editor: QComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QComboBox, index):
        current = str(index.data(Qt.EditRole) or "")
        editor.blockSignals(True)
        i = editor.findData(current)
        if i >= 0:
            editor.setCurrentIndex(i)
        editor.blockSignals(False)
        editor.showPopup()

    def setModelData(self, editor: QComboBox, model, index):
        model.setData(index, editor.currentData(), Qt.EditRole)
