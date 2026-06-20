"""Reusable checklist widget with search + select-all / deselect-all.

A vertical panel: a label header, a search box, two small "Select all" /
"Deselect all" links, and a scrollable :class:`QListWidget` whose items
are all checkable. Used by the spending-report filter dialog (ADR-039
follow-up) for Accounts / Categories / Payees — all three needed the
same UX shape (large lists, find-quickly, bulk-toggle).

The widget owns its items but exposes the ones the caller cares about
through :py:meth:`checked_ids` + :py:meth:`set_checked_ids`. Items carry
their numeric id in ``Qt.UserRole``.

Search is plain substring (case-insensitive). Hidden items aren't
affected by Select all / Deselect all — those verbs only touch the
currently-visible subset, matching how most apps handle search-scoped
bulk-toggle (the user filters, then bulks).
"""
from __future__ import annotations
from mfl_desktop.ui import tokens

from typing import Iterable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class CheckListPanel(QWidget):
    """Search-enabled checklist with bulk-toggle verbs.

    Construct with the full set of ``(id, label)`` rows; the widget renders
    every row checked by default. Callers seed an explicit subset via
    :py:meth:`set_checked_ids` after construction when restoring saved
    filters.

    Emits :py:attr:`changed` whenever a check-state changes (debounce-free
    — the upstream Spending window's refresh is cheap enough).
    """

    changed = Signal()

    def __init__(
        self,
        title: str,
        rows: list[tuple[int, str]],
        *,
        placeholder: str = "Search…",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._all_rows: list[tuple[int, str]] = list(rows)

        title_label = QLabel(title)
        tokens.themed(title_label, "font-weight: bold; color: {heading}; padding-top: 4px;")

        self._search = QLineEdit()
        self._search.setPlaceholderText(placeholder)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_text_changed)

        # Two small flat-style action buttons. Real <a> tags would be
        # nicer but Qt's QLabel link-handling is heavier than this.
        self._select_all_btn = QPushButton("Select all")
        self._deselect_all_btn = QPushButton("Deselect all")
        for btn in (self._select_all_btn, self._deselect_all_btn):
            btn.setFlat(True)
            btn.setCursor(Qt.PointingHandCursor)
            tokens.themed(btn, "QPushButton { color: {accent}; padding: 0 4px; }QPushButton:hover { text-decoration: underline; }")
            btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self._select_all_btn.clicked.connect(self._on_select_all_visible)
        self._deselect_all_btn.clicked.connect(self._on_deselect_all_visible)

        verbs = QHBoxLayout()
        verbs.setContentsMargins(0, 0, 0, 0)
        verbs.setSpacing(8)
        verbs.addWidget(self._select_all_btn)
        verbs.addWidget(self._deselect_all_btn)
        verbs.addStretch(1)

        self._list = QListWidget()
        self._list.setUniformItemSizes(True)
        self._list.itemChanged.connect(self._on_item_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(title_label)
        layout.addWidget(self._search)
        layout.addLayout(verbs)
        layout.addWidget(self._list, stretch=1)

        self._populate(self._all_rows)

    # ── population ──

    def _populate(self, rows: Iterable[tuple[int, str]]) -> None:
        self._list.blockSignals(True)
        try:
            self._list.clear()
            for rid, label in rows:
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, int(rid))
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
                self._list.addItem(item)
        finally:
            self._list.blockSignals(False)

    def replace_rows(self, rows: list[tuple[int, str]]) -> None:
        """Swap the underlying row set (e.g. when the rollup combo on
        a parent dialog changes the category bucket-id set). All rows
        start checked; callers re-apply any saved subset afterwards."""
        self._all_rows = list(rows)
        # Actually rebuild the visible list — _apply_filter only shows/hides
        # the *existing* items, so without this the widget kept the prior
        # rollup's rows until the dialog was reopened.
        self._populate(self._all_rows)
        # Re-apply any active search filter against the new rows.
        self._apply_filter(self._search.text())
        self.changed.emit()

    # ── checked-state API ──

    def checked_ids(self) -> list[int]:
        """All checked ids regardless of search filter — search hides
        items visually but keeps their underlying check state intact."""
        out: list[int] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.Checked:
                out.append(int(item.data(Qt.UserRole)))
        return out

    def all_ids(self) -> list[int]:
        """Every row id, in the order rendered."""
        return [int(rid) for rid, _ in self._all_rows]

    def is_all_checked(self) -> bool:
        return self.checked_ids() == self.all_ids()

    def set_checked_ids(
        self, ids: Optional[Iterable[int]],
    ) -> None:
        """Set the checked subset. ``None`` or an empty iterable means
        "all checked" (matches the saved-filter convention where empty
        == all)."""
        target: Optional[set[int]] = set(ids) if ids else None
        self._list.blockSignals(True)
        try:
            for i in range(self._list.count()):
                item = self._list.item(i)
                rid = int(item.data(Qt.UserRole))
                if target is None or rid in target:
                    item.setCheckState(Qt.Checked)
                else:
                    item.setCheckState(Qt.Unchecked)
        finally:
            self._list.blockSignals(False)
        self.changed.emit()

    # ── handlers ──

    def _on_search_text_changed(self, text: str) -> None:
        self._apply_filter(text)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            label = item.text().lower()
            item.setHidden(bool(needle) and needle not in label)

    def _on_select_all_visible(self) -> None:
        self._list.blockSignals(True)
        try:
            for i in range(self._list.count()):
                item = self._list.item(i)
                if not item.isHidden():
                    item.setCheckState(Qt.Checked)
        finally:
            self._list.blockSignals(False)
        self.changed.emit()

    def _on_deselect_all_visible(self) -> None:
        self._list.blockSignals(True)
        try:
            for i in range(self._list.count()):
                item = self._list.item(i)
                if not item.isHidden():
                    item.setCheckState(Qt.Unchecked)
        finally:
            self._list.blockSignals(False)
        self.changed.emit()

    def _on_item_changed(self, _item: QListWidgetItem) -> None:
        self.changed.emit()
