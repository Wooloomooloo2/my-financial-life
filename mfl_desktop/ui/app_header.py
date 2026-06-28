"""App header bar (ADR-119).

Replaces the native ``QMenuBar`` + the ADR-116 quick-action ``QToolBar`` with a
single flat, themed strip::

    [Home] [File ▾] [Transaction ▾] … [Help ▾]        [Update ▾]  [◐]  (MH)

The dropdowns host the *same* ``QMenu`` objects built from the *same*
``QAction``s as before (so every handler and shortcut is unchanged) — they are
just shown on flat ``QToolButton``s with an instant popup instead of an OS menu
bar. Left-side buttons accumulate before a stretch; right-side widgets (the
folded-in Update actions, the dark-mode toggle, a person chip) sit after it.

All styling is by object name / dynamic property in ``ui/theme.py`` so the bar
follows the light/dark theme live.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QHBoxLayout, QMenu, QToolButton, QWidget

from mfl_desktop.ui import tokens

# A down-chevron appended to dropdown buttons. The native QToolButton menu
# indicator is hidden in QSS (it draws in an awkward spot on a flat button), so
# this is the affordance that a click opens a menu.
_CHEVRON = "  ▾"


class AppHeader(QWidget):
    """The flat top strip. Build with ``add_menu_button`` / ``add_action_button``
    (left) and ``add_right_menu_button`` / ``add_right_widget`` (right)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("appHeader")
        tokens.themed(
            self,
            "QWidget#appHeader { background: {surface}; "
            "border-bottom: 1px solid {border}; }",
        )
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(8, 4, 10, 4)
        self._row.setSpacing(2)
        # A stretch divides the left (menu) cluster from the right (utility)
        # cluster. Left buttons are inserted *before* it; right widgets are
        # appended *after* it.
        self._row.addStretch(1)
        self._stretch_index = self._row.count() - 1

    # ── left cluster: menu + direct-action buttons ──────────────────────────

    def add_menu_button(self, text: str, menu: QMenu) -> QToolButton:
        """A flat dropdown button (e.g. ``File ▾``) that pops ``menu``."""
        btn = self._make_button(text + _CHEVRON)
        btn.setMenu(menu)
        btn.setPopupMode(QToolButton.InstantPopup)
        self._insert_left(btn)
        return btn

    def add_action_button(self, action: QAction, label: str | None = None) -> QToolButton:
        """A flat button that fires a single ``action`` directly (no menu)."""
        btn = self._make_button(label if label is not None else action.text())
        btn.setDefaultAction(action)
        if label is not None:
            btn.setText(label)
        self._insert_left(btn)
        return btn

    # ── right cluster: utilities ────────────────────────────────────────────

    def add_right_menu_button(self, text: str, menu: QMenu) -> QToolButton:
        btn = self._make_button(text + _CHEVRON)
        btn.setMenu(menu)
        btn.setPopupMode(QToolButton.InstantPopup)
        self._row.addWidget(btn, 0, Qt.AlignVCenter)
        return btn

    def add_right_action_button(self, action: QAction) -> QToolButton:
        """A flat (optionally checkable) button driven by ``action`` — used for
        the dark-mode toggle, which keeps the action's checked state."""
        btn = self._make_button(action.text())
        btn.setDefaultAction(action)
        self._row.addWidget(btn, 0, Qt.AlignVCenter)
        return btn

    def add_right_widget(self, w: QWidget) -> None:
        self._row.addWidget(w, 0, Qt.AlignVCenter)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _make_button(self, text: str) -> QToolButton:
        btn = QToolButton(self)
        btn.setText(text.replace("&", ""))   # strip menu-mnemonic ampersands
        btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        btn.setAutoRaise(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setProperty("mflHeaderButton", True)
        return btn

    def _insert_left(self, btn: QToolButton) -> None:
        self._row.insertWidget(self._stretch_index, btn)
        self._stretch_index += 1
