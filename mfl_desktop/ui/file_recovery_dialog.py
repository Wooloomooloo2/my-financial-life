"""Launch-time file-recovery dialog (ADR-109).

Shown *before* the main window when the user's configured main file can't be
read at launch — typically a cloud-synced file (iCloud / OneDrive / Dropbox /
Google Drive) that hasn't been downloaded to this device yet, or a removable /
network drive that isn't mounted.

The whole point of ADR-109 is that the app must **never silently open a
different file** in this situation (the bug that lost a day of edits). So instead
of falling back, we stop and let the user decide explicitly:

- **Retry** — they've started their cloud app / plugged the drive back in.
- **Open a different file…** — point at another ``.mfl`` to work in.
- **Start a new file…** — begin fresh (the missing file is left untouched and
  will be reopened next launch if it comes back, since the pointer is only
  moved on an explicit choice here).

The dialog has no parent (the main window doesn't exist yet) and is app-modal.
:meth:`run` returns a :class:`RecoveryChoice` describing what the user picked;
the launch resolver acts on it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

_DB_FILTER = "My Financial Life databases (*.mfl *.db);;All files (*)"


@dataclass(frozen=True)
class RecoveryChoice:
    """What the user chose in the recovery dialog.

    Exactly one of the booleans is True, or all are False when the user closed
    the dialog (a request to quit). ``path`` carries the chosen file for
    ``open_other`` / ``new_file``."""

    retry: bool = False
    open_other: bool = False
    new_file: bool = False
    path: Optional[Path] = None


class FileRecoveryDialog(QDialog):
    """Blocking 'your data file isn't available' recovery prompt. See module docstring."""

    def __init__(self, missing_path: Path, reason: str = "unavailable") -> None:
        super().__init__(None)  # no parent — shown before the main window exists
        self._missing = Path(missing_path)
        self._choice = RecoveryChoice()
        self.setWindowTitle("Can’t open your data file")
        self.setModal(True)

        root = QVBoxLayout(self)

        if reason == "unreadable":
            headline = "Your data file couldn’t be opened."
            detail = (
                f"The file at:\n\n{self._missing}\n\n"
                "exists but couldn’t be read as a My Financial Life database — it "
                "may still be downloading from your cloud storage, or it may be "
                "damaged. Wait a moment and Retry, or choose another file below."
            )
        else:
            headline = "Your data file isn’t available right now."
            detail = (
                f"My Financial Life couldn’t find:\n\n{self._missing}\n\n"
                "If this file lives in a synced folder (iCloud Drive, OneDrive, "
                "Dropbox, Google Drive…), it may not be downloaded to this device "
                "yet. Make sure that app is running and online, then Retry. "
                "Otherwise you can open a different file or start a new one.\n\n"
                "Your file is left untouched — it will reopen automatically once "
                "it’s back."
            )

        title = QLabel(headline)
        title.setStyleSheet("font-weight: 600;")
        title.setWordWrap(True)
        root.addWidget(title)

        body = QLabel(detail)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(body)

        row = QHBoxLayout()
        retry_btn = QPushButton("Retry")
        retry_btn.setDefault(True)
        retry_btn.clicked.connect(self._on_retry)
        open_btn = QPushButton("Open a different file…")
        open_btn.clicked.connect(self._on_open_other)
        new_btn = QPushButton("Start a new file…")
        new_btn.clicked.connect(self._on_new_file)
        row.addWidget(open_btn)
        row.addWidget(new_btn)
        row.addStretch(1)
        row.addWidget(retry_btn)
        root.addLayout(row)

    # ── button handlers ──

    def _on_retry(self) -> None:
        self._choice = RecoveryChoice(retry=True)
        self.accept()

    def _on_open_other(self) -> None:
        start_dir = str(self._missing.parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "Open My Financial Life database", start_dir, _DB_FILTER
        )
        if not path:
            return  # cancelled the picker — stay on the recovery dialog
        self._choice = RecoveryChoice(open_other=True, path=Path(path))
        self.accept()

    def _on_new_file(self) -> None:
        # Default the new file into the first-run location so a brand-new file
        # lands somewhere visible and sensible (ADR-109).
        from mfl_desktop.launch import first_run_default_path

        default = first_run_default_path()
        try:
            default.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        path, _ = QFileDialog.getSaveFileName(
            self, "Start a new data file", str(default),
            "My Financial Life databases (*.mfl);;All files (*)",
        )
        if not path:
            return
        new_path = Path(path)
        if new_path.suffix == "":
            new_path = new_path.with_suffix(".mfl")
        self._choice = RecoveryChoice(new_file=True, path=new_path)
        self.accept()

    def run(self) -> RecoveryChoice:
        """Show the dialog modally and return the user's choice. Closing the
        window (rejecting) returns an all-False choice = 'quit'."""
        self.exec()
        return self._choice
