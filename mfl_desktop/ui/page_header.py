"""Contextual page header (ADR-119).

The MRL-style title + grey subtitle that opens every screen, with a slot on the
right for the page's primary action(s). Sits at the top of the content column,
below the app header and above the register / Home stack. ``set_heading`` is
driven from ``RegisterWindow._update_window_title`` so it always matches the
active view.
"""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from mfl_desktop.ui import tokens


class PageHeader(QWidget):
    def __init__(self, parent=None, *, show_rule: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("pageHeader")
        # Report windows ask for a hairline under the header to separate it from
        # the chart/table below (they used to draw their own top rule); the
        # register's header sits above a framed grid and wants none.
        rule = " border-bottom: 1px solid {border};" if show_rule else ""
        tokens.themed(
            self, "QWidget#pageHeader { background: {canvas};" + rule + " }"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(20, 14, 20, 8)
        row.setSpacing(12)
        self._row = row

        # Leading slot — a back affordance sits left of the title (filled by the
        # owning window only when it has one; empty otherwise).
        self._leading = QHBoxLayout()
        self._leading.setContentsMargins(0, 0, 0, 0)
        self._leading.setSpacing(8)
        row.addLayout(self._leading, 0)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        self._title = QLabel("")
        tokens.themed(self._title, "color: {text}; font-size: 20px; font-weight: 600;")
        self._subtitle = QLabel("")
        tokens.themed(self._subtitle, "color: {muted}; font-size: 12px;")
        text_col.addWidget(self._title)
        text_col.addWidget(self._subtitle)
        row.addLayout(text_col, 1)

        # Right-hand slot for page-level primary actions (filled later by the
        # owning window if a screen wants one in the header).
        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(8)
        row.addLayout(self._actions, 0)

    def set_heading(self, title: str, subtitle: str = "") -> None:
        self._title.setText(title)
        self._subtitle.setText(subtitle)
        self._subtitle.setVisible(bool(subtitle))

    def add_action(self, w: QWidget) -> None:
        self._actions.addWidget(w)

    def add_leading(self, w: QWidget) -> None:
        """Add a widget left of the title (e.g. a drill-down Back button)."""
        self._leading.addWidget(w)
