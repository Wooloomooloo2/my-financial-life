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


def report_folder_name(repo, folder_id: int | None) -> str | None:
    """The name of the report folder ``folder_id`` sits in, or None.

    The same four-line scan over ``list_report_folders()`` was inlined in all
    five report windows; this is the one copy (ADR-164).
    """
    if folder_id is None:
        return None
    for folder in repo.list_report_folders():
        if folder.id == folder_id:
            return folder.name
    return None


def report_heading(
    type_label: str,
    loaded_name: str | None,
    *,
    folder_name: str | None = None,
    dirty: bool = False,
) -> tuple[str, str, str]:
    """``(title, subtitle, window_title)`` for a report window's header (ADR-164).

    An *unsaved* report used to lead with the word **"Untitled"** — the largest
    text on the screen — while the thing the report actually *is* ("Spending
    Over Time") was the small grey subtitle underneath. That is backwards: the
    report's type is its identity until you give it a name, and "Untitled" is a
    statement about the *file*, not about what you are looking at.

    So an unsaved report leads with its type and carries its unsaved-ness as a
    quiet subtitle. A saved one leads with its name — which is the identity the
    user chose — and keeps the type as the subtitle.

    Five report windows had grown their own copy of this string-building; this
    is the single definition (the ADR-084 rule: consolidate divergent duplicates
    of the same thing).
    """
    if loaded_name is None:
        return type_label, "Unsaved report", f"{type_label} — Unsaved"
    prefix = f"{folder_name} / " if folder_name else ""
    title = f"{prefix}{loaded_name}{'*' if dirty else ''}"
    return title, type_label, f"{type_label} — {title}"


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
