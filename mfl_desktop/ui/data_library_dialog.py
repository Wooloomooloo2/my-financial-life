"""The Data Library screen — visual save/load of whole datasets (ADR-059).

A small modal that lists the user's saved ``.mfl`` datasets and the automatic
ADR-057 snapshots side by side, so test data can be juggled without the two
blind file-pickers (``File ▸ Open…`` / ``File ▸ Save Copy As…``). Two tabs:

- **Saved datasets** — named copies the user keeps on purpose. Verbs: *Load*
  (a fresh working copy — the saved original stays pristine), *Save current
  as…*, *Rename…*, *Delete*.
- **Snapshots** — the rotating automatic backups. Verbs: *Load a copy*,
  *Delete*.

All at-rest file work lives in :mod:`mfl_desktop.data_library`; this dialog is
just the UI over it. Loading is the one thing the dialog can't do itself — it
replaces the live working file, which only the owning window can orchestrate —
so a load emits :attr:`load_requested` and closes; the window does the swap.
Saving uses the live ``Repository.save_copy`` (atomic backup of the current
data) and leaves the working file untouched, so the dialog stays open.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialogButtonBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop import data_library
from mfl_desktop.data_library import DataFile
from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.locations_dialog import LocationsDialog
from mfl_desktop.ui.snapshot_settings_dialog import SnapshotSettingsDialog

_FILE_ROLE = Qt.UserRole


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _fmt_when(when: datetime) -> str:
    return when.strftime("%d %b %Y  %H:%M")


class DataLibraryDialog(QDialog):
    """Manage saved datasets + snapshots. See module docstring."""

    # Emitted when the user confirms a load. The path is a saved dataset or a
    # snapshot; the owning window clones it onto the live working file. The
    # dialog closes immediately after — its repo reference is about to go stale.
    load_requested = Signal(Path)
    # Emitted after the user changes snapshot retention settings, so the window
    # can re-arm its capture timer at the new cadence (ADR-060).
    settings_changed = Signal()
    # ADR-109 Locations: the user picked an existing file to make the live file.
    open_existing_main_requested = Signal(Path)
    # ADR-109 Locations: relocate the live file into the picked folder.
    relocate_main_requested = Signal(Path)
    # ADR-109 Locations: new parent folder for the MFL Snapshots directory.
    snapshots_root_changed = Signal(Path)

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Data Library")
        self.setModal(True)
        self.resize(620, 480)

        root = QVBoxLayout(self)

        intro = QLabel(
            "Save the current data as a named dataset, or load a saved dataset "
            "or snapshot. Loading takes a fresh working copy — the saved file "
            "stays untouched, so you can reload it clean any time."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self._tabs = QTabWidget()
        self._saved_table = self._make_table(("Dataset", "Saved", "Size"))
        self._snap_table = self._make_table(("Snapshot", "Taken", "Size"))
        self._tabs.addTab(self._saved_page(), "Saved datasets")
        self._tabs.addTab(self._snapshots_page(), "Snapshots")
        root.addWidget(self._tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        compact_btn = buttons.addButton(
            "Compact file…", QDialogButtonBox.ActionRole
        )
        compact_btn.setToolTip(
            "Reclaim unused space. Deleting data and app updates leave gaps that\n"
            "SQLite doesn't return automatically, so the file only ever grows —\n"
            "compacting rewrites it tightly, keeping all your data."
        )
        compact_btn.clicked.connect(self._on_compact)
        locations_btn = buttons.addButton(
            "Locations…", QDialogButtonBox.ActionRole
        )
        locations_btn.clicked.connect(self._on_locations)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh()

    # ── page construction ──

    def _make_table(self, headers: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(list(headers))
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        table.itemSelectionChanged.connect(self._sync_buttons)
        table.itemDoubleClicked.connect(self._on_double_click)
        return table

    def _saved_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(self._saved_table)

        row = QHBoxLayout()
        self._load_btn = QPushButton("Load…")
        self._load_btn.clicked.connect(self._on_load_saved)
        self._save_as_btn = QPushButton("Save current as…")
        self._save_as_btn.clicked.connect(self._on_save_as)
        self._rename_btn = QPushButton("Rename…")
        self._rename_btn.clicked.connect(self._on_rename)
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._on_delete_saved)
        row.addWidget(self._load_btn)
        row.addWidget(self._save_as_btn)
        row.addStretch(1)
        row.addWidget(self._rename_btn)
        row.addWidget(self._delete_btn)
        layout.addLayout(row)
        return page

    def _snapshots_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(self._snap_table)

        row = QHBoxLayout()
        self._snap_load_btn = QPushButton("Load a copy…")
        self._snap_load_btn.clicked.connect(self._on_load_snapshot)
        self._snap_settings_btn = QPushButton("Settings…")
        self._snap_settings_btn.clicked.connect(self._on_snapshot_settings)
        self._snap_delete_btn = QPushButton("Delete")
        self._snap_delete_btn.clicked.connect(self._on_delete_snapshot)
        row.addWidget(self._snap_load_btn)
        row.addWidget(self._snap_settings_btn)
        row.addStretch(1)
        row.addWidget(self._snap_delete_btn)
        layout.addLayout(row)
        return page

    # ── data ──

    def _refresh(self) -> None:
        db_path = self._repo.db_path
        # The live working file is pinned at the top so the user can always see
        # which file they're editing (ADR-109), above their named saved copies.
        saved = [data_library.current_file(db_path), *data_library.list_saved(db_path)]
        self._fill(self._saved_table, saved)
        self._fill(self._snap_table, data_library.list_snapshots(db_path))
        self._sync_buttons()

    def _fill(self, table: QTableWidget, files: list[DataFile]) -> None:
        table.setRowCount(len(files))
        for r, f in enumerate(files):
            label = f"{f.name}  (current)" if f.kind == "current" else f.name
            name = QTableWidgetItem(label)
            name.setData(_FILE_ROLE, f)
            if f.kind == "current":
                font = name.font()
                font.setBold(True)
                name.setFont(font)
            when = QTableWidgetItem(_fmt_when(f.saved_at))
            size = QTableWidgetItem(_fmt_size(f.size))
            size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(r, 0, name)
            table.setItem(r, 1, when)
            table.setItem(r, 2, size)

    def _selected(self, table: QTableWidget) -> Optional[DataFile]:
        rows = table.selectionModel().selectedRows()
        if not rows:
            return None
        item = table.item(rows[0].row(), 0)
        return item.data(_FILE_ROLE) if item else None

    def _sync_buttons(self) -> None:
        selected_saved = self._selected(self._saved_table)
        # The pinned "current" row is the live file itself — it can't be loaded
        # onto itself, renamed, or deleted (ADR-109).
        is_real_saved = (
            selected_saved is not None and selected_saved.kind == "saved"
        )
        self._load_btn.setEnabled(is_real_saved)
        self._rename_btn.setEnabled(is_real_saved)
        self._delete_btn.setEnabled(is_real_saved)
        has_snap = self._selected(self._snap_table) is not None
        self._snap_load_btn.setEnabled(has_snap)
        self._snap_delete_btn.setEnabled(has_snap)

    # ── actions ──

    def _on_double_click(self, _item: QTableWidgetItem) -> None:
        table = self.sender()
        chosen = self._selected(table)
        # The pinned "current" row is the live file — double-clicking it is a
        # no-op (you can't load the file you're already in onto itself).
        if chosen is not None and chosen.kind != "current":
            self._load(chosen)

    def _on_compact(self) -> None:
        """Reclaim free pages in the live file via ``Repository.compact`` (VACUUM).
        Shows the before/after size so the reclaim is visible."""
        try:
            size = self._repo.db_path.stat().st_size
        except OSError:
            size = 0
        confirm = QMessageBox.question(
            self, "Compact file",
            "Reclaim unused space in this file?\n\n"
            "Deleting data (merged payees, removed accounts) and app updates "
            "leave gaps that SQLite doesn't return to disk on its own, so the "
            "file only ever grows. Compacting rewrites it tightly and keeps "
            "every bit of your data.\n\n"
            f"Current size: {_fmt_size(size)}.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            before, after = self._repo.compact()
        except Exception as e:  # noqa: BLE001 — surface any VACUUM failure
            QMessageBox.critical(
                self, "Compact failed",
                f"Could not compact the file:\n\n{e}",
            )
            return
        saved = before - after
        if saved > 0:
            msg = (
                f"Reclaimed {_fmt_size(saved)}.\n\n"
                f"{_fmt_size(before)}  →  {_fmt_size(after)}"
            )
        else:
            msg = "The file was already compact — nothing to reclaim."
        QMessageBox.information(self, "Compacted", msg)
        self._refresh()

    def _on_locations(self) -> None:
        dialog = LocationsDialog(self._repo, parent=self)
        # Snapshot-folder change is safe to apply while we stay open: forward it
        # to the window (which sets the root + captures into the new location),
        # then refresh our snapshot list to show the move.
        dialog.snapshots_root_changed.connect(self.snapshots_root_changed)
        dialog.snapshots_root_changed.connect(lambda _p: self._refresh())
        # The two main-file actions swap the live file, so close this dialog
        # first — our repo handle is about to be torn down (mirrors _load).
        dialog.open_existing_main_requested.connect(self._forward_open_existing)
        dialog.relocate_main_requested.connect(self._forward_relocate)
        dialog.exec()

    def _forward_open_existing(self, path: Path) -> None:
        self.accept()
        self.open_existing_main_requested.emit(path)

    def _forward_relocate(self, target_dir: Path) -> None:
        self.accept()
        self.relocate_main_requested.emit(target_dir)

    def _on_load_saved(self) -> None:
        chosen = self._selected(self._saved_table)
        if chosen is not None:
            self._load(chosen)

    def _on_load_snapshot(self) -> None:
        chosen = self._selected(self._snap_table)
        if chosen is not None:
            self._load(chosen)

    def _on_snapshot_settings(self) -> None:
        dialog = SnapshotSettingsDialog(self._repo, parent=self)
        # Saving applies the new policy (prunes the existing set), so refresh the
        # snapshot list, and let the window re-arm its capture timer (ADR-060).
        dialog.policy_saved.connect(self._refresh)
        dialog.policy_saved.connect(self.settings_changed)
        dialog.exec()

    def _load(self, file: DataFile) -> None:
        noun = "dataset" if file.kind == "saved" else "snapshot"
        confirm = QMessageBox.question(
            self,
            f"Load {noun}",
            f"Load “{file.name}” as a fresh working copy?\n\n"
            "Your current data is backed up to a snapshot first, then replaced. "
            f"The saved {noun} itself is left untouched.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        # The window owns the live file, so it performs the swap. Close first —
        # our repo handle is about to be torn down underneath us.
        self.accept()
        self.load_requested.emit(file.path)

    def _on_save_as(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Save current data as", "Dataset name:"
        )
        if not ok:
            return
        stem = data_library.sanitize_name(name)
        if not stem:
            QMessageBox.warning(
                self, "Name needed",
                "Enter a name for the dataset.",
            )
            return
        dest = data_library.library_path(self._repo.db_path, stem)
        if dest.exists():
            overwrite = QMessageBox.question(
                self, "Replace dataset",
                f"A dataset named “{stem}” already exists. Replace it?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if overwrite != QMessageBox.Yes:
                return
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._repo.save_copy(dest)
        except Exception as e:  # noqa: BLE001 — surface any backup failure
            QMessageBox.critical(
                self, "Save failed",
                f"Could not save “{stem}”:\n\n{e}",
            )
            return
        self._refresh()
        self._select_by_name(self._saved_table, stem)

    def _on_rename(self) -> None:
        chosen = self._selected(self._saved_table)
        if chosen is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename dataset", "New name:", text=chosen.name
        )
        if not ok:
            return
        try:
            data_library.rename_saved(chosen.path, new_name)
        except ValueError:
            QMessageBox.warning(self, "Name needed", "Enter a name.")
            return
        except FileExistsError:
            QMessageBox.warning(
                self, "Name taken",
                "Another dataset already has that name.",
            )
            return
        except OSError as e:
            QMessageBox.critical(self, "Rename failed", str(e))
            return
        self._refresh()
        self._select_by_name(
            self._saved_table, data_library.sanitize_name(new_name)
        )

    def _on_delete_saved(self) -> None:
        self._delete(self._saved_table, "dataset")

    def _on_delete_snapshot(self) -> None:
        self._delete(self._snap_table, "snapshot")

    def _delete(self, table: QTableWidget, noun: str) -> None:
        chosen = self._selected(table)
        if chosen is None:
            return
        confirm = QMessageBox.question(
            self, f"Delete {noun}",
            f"Delete “{chosen.name}”? This can't be undone.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            data_library.delete_file(chosen.path)
        except OSError as e:
            QMessageBox.critical(self, "Delete failed", str(e))
            return
        self._refresh()

    def _select_by_name(self, table: QTableWidget, name: str) -> None:
        for r in range(table.rowCount()):
            item = table.item(r, 0)
            if item and item.text() == name:
                table.selectRow(r)
                return
