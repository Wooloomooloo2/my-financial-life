"""Lockable secret field (ADR-127).

A password ``QLineEdit`` that starts **read-only and greyed** when it already
holds a value, with a small **Change** button that unlocks it for editing — so
a stored API key sitting at the top of a settings screen can't be overwritten
by an accidental click-and-type. A fresh (empty) field starts unlocked so a
first-time user can paste straight in.

Used by the Securities dialog (Tiingo token) and the Currencies dialog
(OpenExchangeRates token), whose key fields were otherwise always editable.

Reads exactly like a plain line edit — call ``.text()`` for the current value.
The greyed look is token-driven (ADR-076), so it tracks light/dark theme.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from mfl_desktop.ui import tokens

# Read-only "locked" appearance: a muted fill + muted text so the field clearly
# reads as non-editable, in both themes. Cleared (back to the global QSS) when
# unlocked. QSS blocks pass through tokens._format untouched; only {token}
# placeholders are substituted.
_LOCKED_QSS = (
    "QLineEdit { background: {surface_alt}; color: {muted_strong};"
    " border: 1px solid {border}; }"
)


class LockableSecretField(QWidget):
    """Password line edit + a Change-to-edit lock.

    Locks automatically when seeded with a non-empty ``value``; unlocked while
    empty. ``line_edit`` is exposed so existing call sites can keep a reference
    to the inner ``QLineEdit`` (``.text()`` is also proxied here)."""

    def __init__(
        self,
        *,
        placeholder: str = "",
        value: str = "",
        change_tooltip: str = "Unlock this field to replace the stored value.",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.line_edit = QLineEdit()
        self.line_edit.setEchoMode(QLineEdit.Password)
        if placeholder:
            self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setText(value)

        self._change_btn = QPushButton("Change")
        self._change_btn.setToolTip(change_tooltip)
        # autoDefault off so this button never steals Enter from the dialog's
        # default (Save / Refresh) button.
        self._change_btn.setAutoDefault(False)
        self._change_btn.clicked.connect(lambda: self.set_locked(False))

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.line_edit, 1)
        row.addWidget(self._change_btn)

        self.set_locked(bool(value.strip()))

    # ── plain-line-edit passthroughs ─────────────────────────────────────
    def text(self) -> str:
        return self.line_edit.text()

    def setText(self, value: str) -> None:  # noqa: N802 — Qt casing
        self.line_edit.setText(value)

    # ── lock state ───────────────────────────────────────────────────────
    def is_locked(self) -> bool:
        return self.line_edit.isReadOnly()

    def set_locked(self, locked: bool) -> None:
        """Lock (read-only + greyed, Change button shown) or unlock (editable,
        focused with the text selected, Change button hidden)."""
        self.line_edit.setReadOnly(locked)
        self._change_btn.setVisible(locked)
        if locked:
            tokens.themed(self.line_edit, _LOCKED_QSS)
        else:
            tokens.themed(self.line_edit, "")  # restore the global QSS look
            self.line_edit.setFocus()
            self.line_edit.selectAll()
