"""Edit-your-profile dialog (ADR-119).

Sets the account holder's display name, which drives the header avatar initials
and personalises the app. Reached by clicking the avatar chip in the app header.
Shows a live initials preview as you type so it's obvious what the avatar will
become.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from mfl_desktop.ui import tokens


def initials_for(name: str) -> str:
    """Two-letter avatar initials for a display name (shared rule)."""
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "ME"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


class ProfileDialog(QDialog):
    def __init__(self, current_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Your profile")
        self.setMinimumWidth(380)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(14)

        intro = QLabel(
            "Your name personalises the app and sets the initials shown in the "
            "avatar at the top-right."
        )
        intro.setWordWrap(True)
        tokens.themed(intro, "color: {muted};")
        root.addWidget(intro)

        row = QHBoxLayout()
        row.setSpacing(12)
        self._preview = QLabel(initials_for(current_name))
        self._preview.setObjectName("personChip")
        self._preview.setFixedSize(44, 44)
        self._preview.setAlignment(Qt.AlignCenter)
        tokens.themed(
            self._preview,
            "QLabel#personChip { background: {accent}; color: {on_accent}; "
            "border-radius: 22px; font-weight: 600; font-size: 16px; }",
        )
        row.addWidget(self._preview, 0)

        col = QVBoxLayout()
        col.setSpacing(4)
        label = QLabel("Your name")
        tokens.themed(label, "font-weight: 600; color: {heading};")
        self._edit = QLineEdit(current_name)
        self._edit.setPlaceholderText("e.g. Mark Hall")
        self._edit.textChanged.connect(
            lambda t: self._preview.setText(initials_for(t))
        )
        col.addWidget(label)
        col.addWidget(self._edit)
        row.addLayout(col, 1)
        root.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        save = buttons.button(QDialogButtonBox.Save)
        if save is not None:
            save.setProperty("mflVariant", "primary")
        root.addWidget(buttons)

        self._edit.setFocus()
        self._edit.selectAll()

    def name(self) -> str:
        return self._edit.text().strip()
