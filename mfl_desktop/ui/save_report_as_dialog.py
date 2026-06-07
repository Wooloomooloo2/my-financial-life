"""Save Report As… dialog (ADR-039).

Small modal: name field + folder picker + Save / Cancel. The folder combo
lists existing :class:`ReportFolderRow`s plus a sentinel "(Root)" entry
for "no folder" and a trailing "New folder…" verb that prompts inline.

Returns a :class:`SaveAsChoice` on Accepted — the caller is responsible
for invoking the Repository (``create_report`` or
``update_report(name=…, folder_id=…)``) with the chosen values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from mfl_desktop.db.repository import Repository

# Sentinel itemData values for the folder combo. Real folder ids are
# positive integers, so the non-int sentinels never collide.
_ROOT_SENTINEL = "__root__"
_NEW_FOLDER_SENTINEL = "__new__"


@dataclass(frozen=True)
class SaveAsChoice:
    """User's pick from the Save As dialog. ``folder_id`` of ``None``
    means the report sits at the Reports-section root."""
    name: str
    folder_id: Optional[int]


class SaveReportAsDialog(QDialog):
    """Two-field modal: name + folder. ``initial_name`` pre-fills the
    name field (Save As on a saved report seeds the existing name); pass
    ``None`` for a bare-window Save (the field starts empty)."""

    def __init__(
        self,
        repo: Repository,
        *,
        initial_name: Optional[str] = None,
        initial_folder_id: Optional[int] = None,
        title: str = "Save report as…",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self._repo = repo
        self._choice: Optional[SaveAsChoice] = None
        # Suppress _on_folder_changed reacting to the synthetic edits we
        # do when rebuilding the combo (otherwise the New-folder prompt
        # can fire mid-rebuild).
        self._suppress_folder_signal = False

        self._name_edit = QLineEdit(initial_name or "")
        self._name_edit.setPlaceholderText("Report name")

        self._folder_combo = QComboBox()
        self._populate_folder_combo(select_folder_id=initial_folder_id)
        self._folder_combo.currentIndexChanged.connect(
            self._on_folder_changed
        )

        form = QFormLayout()
        form.addRow("Name:", self._name_edit)
        form.addRow("Folder:", self._folder_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(buttons)

    def values(self) -> Optional[SaveAsChoice]:
        return self._choice

    # ── folder combo plumbing ──

    def _populate_folder_combo(
        self, *, select_folder_id: Optional[int],
    ) -> None:
        self._suppress_folder_signal = True
        try:
            self._folder_combo.clear()
            self._folder_combo.addItem("(Root — no folder)", userData=_ROOT_SENTINEL)
            folders = self._repo.list_report_folders()
            selected_index = 0
            for i, f in enumerate(folders, start=1):
                self._folder_combo.addItem(f.name, userData=int(f.id))
                if select_folder_id is not None and f.id == select_folder_id:
                    selected_index = i
            self._folder_combo.insertSeparator(self._folder_combo.count())
            self._folder_combo.addItem(
                "New folder…", userData=_NEW_FOLDER_SENTINEL,
            )
            self._folder_combo.setCurrentIndex(selected_index)
        finally:
            self._suppress_folder_signal = False

    def _on_folder_changed(self, _index: int) -> None:
        if self._suppress_folder_signal:
            return
        data = self._folder_combo.currentData()
        if data != _NEW_FOLDER_SENTINEL:
            return
        # User picked the trailing "New folder…" verb — prompt for a name,
        # create it, and select it. On cancel, drop back to Root.
        name, ok = QInputDialog.getText(
            self, "New folder", "Folder name:",
        )
        if not ok or not name.strip():
            self._suppress_folder_signal = True
            try:
                self._folder_combo.setCurrentIndex(0)
            finally:
                self._suppress_folder_signal = False
            return
        try:
            folder = self._repo.create_report_folder(name.strip())
        except Exception as e:
            QMessageBox.critical(
                self, "Could not create folder",
                f"The folder was not created:\n\n{e}",
            )
            self._suppress_folder_signal = True
            try:
                self._folder_combo.setCurrentIndex(0)
            finally:
                self._suppress_folder_signal = False
            return
        self._populate_folder_combo(select_folder_id=folder.id)

    # ── accept ──

    def _on_save(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.information(
                self, "Name required",
                "Please give the report a name before saving.",
            )
            self._name_edit.setFocus()
            return
        data = self._folder_combo.currentData()
        folder_id: Optional[int]
        if data == _ROOT_SENTINEL or data == _NEW_FOLDER_SENTINEL:
            folder_id = None
        else:
            folder_id = int(data)
        self._choice = SaveAsChoice(name=name, folder_id=folder_id)
        self.accept()
