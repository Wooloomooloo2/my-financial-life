"""Data file + backups location settings (ADR-109).

The Manage Data ▸ Locations screen. Two things the user can now control, which
used to be implicit and were the heart of the "which file am I even editing?"
confusion:

- **Main data file** — where the live working file lives. *Open existing…*
  points the app at another ``.mfl`` (it becomes the file reopened on next
  launch); *Move to folder…* relocates the current file to a chosen folder.
- **Snapshots folder** — the parent of the ``MFL Snapshots`` backup folder.
  *Change…* picks a new parent; *Reveal* opens it in the file manager.

This dialog only *collects intent* — the actual file swap / relocate / snapshot
re-point is performed by the owning window (it owns the live ``Repository``), so
each action emits a signal and the window acts. Mirrors the existing
``DataLibraryDialog.load_requested`` pattern.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from mfl_desktop import snapshots
from mfl_desktop.db.repository import Repository

_DB_FILTER = "My Financial Life databases (*.mfl *.db);;All files (*)"


class LocationsDialog(QDialog):
    """Choose the main-file and snapshots-folder locations. See module docstring."""

    # The picked existing file should become the live working file.
    open_existing_main_requested = Signal(Path)
    # Relocate the current working file *into* the picked folder.
    relocate_main_requested = Signal(Path)
    # New parent folder for the ``MFL Snapshots`` directory.
    snapshots_root_changed = Signal(Path)

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Locations")
        self.setModal(True)
        self.resize(560, 0)

        root = QVBoxLayout(self)

        # ── Main data file ──
        root.addWidget(self._section_label("Main data file"))
        root.addWidget(self._hint(
            "The file you’re working in. It’s reopened automatically each time "
            "you start the app."
        ))
        self._file_path = QLabel(str(self._repo.db_path))
        self._file_path.setWordWrap(True)
        self._file_path.setTextInteractionFlags(
            self._file_path.textInteractionFlags()
        )
        root.addWidget(self._file_path)
        file_row = QHBoxLayout()
        open_btn = QPushButton("Open existing…")
        open_btn.clicked.connect(self._on_open_existing)
        move_btn = QPushButton("Move to folder…")
        move_btn.clicked.connect(self._on_move)
        file_row.addWidget(open_btn)
        file_row.addWidget(move_btn)
        file_row.addStretch(1)
        root.addLayout(file_row)

        root.addWidget(self._divider())

        # ── Snapshots folder ──
        root.addWidget(self._section_label("Snapshots folder"))
        root.addWidget(self._hint(
            "Automatic backups are kept here, in a folder called “MFL Snapshots”. "
            "Keeping them off a cloud-synced folder avoids large uploads."
        ))
        self._snap_path = QLabel()
        self._snap_path.setWordWrap(True)
        root.addWidget(self._snap_path)
        snap_row = QHBoxLayout()
        change_btn = QPushButton("Change…")
        change_btn.clicked.connect(self._on_change_snapshots)
        reveal_btn = QPushButton("Reveal")
        reveal_btn.clicked.connect(self._on_reveal_snapshots)
        snap_row.addWidget(change_btn)
        snap_row.addWidget(reveal_btn)
        snap_row.addStretch(1)
        root.addLayout(snap_row)
        self._refresh_snap_path()

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── small builders ──

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("font-weight: 600;")
        return lbl

    def _hint(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        return lbl

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _snapshot_folder(self) -> Path:
        return snapshots.snapshot_dir(self._repo.db_path)

    def _refresh_snap_path(self) -> None:
        self._snap_path.setText(str(self._snapshot_folder()))

    # ── actions ──

    def _on_open_existing(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open My Financial Life database",
            str(self._repo.db_path.parent), _DB_FILTER,
        )
        if not path:
            return
        if Path(path).resolve() == Path(self._repo.db_path).resolve():
            return  # already the live file — nothing to do
        # Close before emitting — the live file is about to be swapped out from
        # under us, so our repo handle goes stale (mirrors DataLibraryDialog).
        self.accept()
        self.open_existing_main_requested.emit(Path(path))

    def _on_move(self) -> None:
        target = QFileDialog.getExistingDirectory(
            self, "Move data file to folder", str(self._repo.db_path.parent),
        )
        if not target:
            return
        if Path(target).resolve() == Path(self._repo.db_path).parent.resolve():
            return  # same folder — nothing to do
        self.accept()  # repo about to be swapped to the relocated copy
        self.relocate_main_requested.emit(Path(target))

    def _on_change_snapshots(self) -> None:
        current_parent = str(self._snapshot_folder().parent)
        target = QFileDialog.getExistingDirectory(
            self, "Choose where backups are kept", current_parent,
        )
        if not target:
            return
        self.snapshots_root_changed.emit(Path(target))
        self._refresh_snap_path()

    def _on_reveal_snapshots(self) -> None:
        folder = self._snapshot_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
