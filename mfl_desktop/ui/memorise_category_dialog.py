"""Confirm dialog for remembering a payee→category mapping (ADR-072).

Shown after the user sets a category on a register row whose payee has no
auto-category yet. Offers to remember the mapping (so future imports of that
payee are auto-categorised) and, when uncategorised transactions for that
payee already exist, to apply it to them too — uncategorised only, never
overwriting a category the user already set.

The dialog is a plain confirm: accept means "remember"; the checkbox (shown
only when there's existing history to touch) carries the retroactive choice.
"""
from __future__ import annotations

from html import escape

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)


class MemoriseCategoryDialog(QDialog):
    def __init__(
        self,
        payee_name: str,
        category_label: str,
        existing_count: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remember category")
        self.setModal(True)

        safe_payee = escape(payee_name)
        safe_cat = escape(category_label)
        msg = QLabel(
            f"Always categorise <b>{safe_payee}</b> as <b>{safe_cat}</b>?"
            f"<br><br>"
            f"New {safe_payee} transactions will be auto-categorised when "
            f"imported."
        )
        msg.setWordWrap(True)

        self._has_existing = existing_count > 0
        self._apply_existing = QCheckBox(
            f"Also apply to {existing_count:,} existing uncategorised "
            f"{payee_name} transaction"
            f"{'s' if existing_count != 1 else ''}"
        )
        self._apply_existing.setChecked(True)

        buttons = QDialogButtonBox()
        buttons.addButton("Remember", QDialogButtonBox.AcceptRole)
        buttons.addButton("Not now", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(msg)
        if self._has_existing:
            layout.addWidget(self._apply_existing)
        layout.addWidget(buttons)

    def apply_to_existing(self) -> bool:
        """True when the user accepted *and* asked to back-fill existing
        uncategorised transactions."""
        return self._has_existing and self._apply_existing.isChecked()
