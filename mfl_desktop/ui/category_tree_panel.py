"""Hierarchical checklist for categories (ADR-056 follow-up).

A :class:`QTreeWidget` cousin of :class:`CheckListPanel`: it shows the category
hierarchy with tri-state checkboxes, so checking a parent selects every
descendant and unchecking it clears them — and a parent shows the partially-
checked state when only some of its children are on. Built for the Sankey
report's category filter, where the owner wants to toggle a whole income/expense
subtree in one click rather than hunting leaf-by-leaf.

The panel owns its tree; callers read/write the selection through
:py:meth:`checked_ids` / :py:meth:`set_checked_ids`. A category is "selected"
only when its own box is fully Checked — a partially-checked parent is *not*
selected itself (its own directly-attached transactions are excluded), but its
checked descendants are. Items carry their numeric id in ``Qt.UserRole``.

Search is plain substring (case-insensitive); a row stays visible if it, any
ancestor, or any descendant matches, so a matched leaf is always reachable from
its root.
"""
from __future__ import annotations
from mfl_desktop.ui import tokens

from typing import Iterable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)


class CategoryTreePanel(QWidget):
    """Search-enabled hierarchical checklist with cascading tri-state checks.

    Construct with ``rows`` as ``(id, parent_id, label)`` tuples. A row whose
    ``parent_id`` is ``None`` (or points outside the row set) becomes a tree
    root. Every row starts checked; restore a saved subset with
    :py:meth:`set_checked_ids`.
    """

    changed = Signal()

    def __init__(
        self,
        title: str,
        rows: list[tuple[int, Optional[int], str]],
        *,
        placeholder: str = "Search categories…",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._all_ids: list[int] = [rid for rid, _, _ in rows]
        self._items: dict[int, QTreeWidgetItem] = {}

        title_label = QLabel(title)
        tokens.themed(title_label, "font-weight: bold; color: {heading}; padding-top: 4px;")

        self._search = QLineEdit()
        self._search.setPlaceholderText(placeholder)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_text_changed)

        self._select_all_btn = QPushButton("Select all")
        self._deselect_all_btn = QPushButton("Deselect all")
        for btn in (self._select_all_btn, self._deselect_all_btn):
            btn.setFlat(True)
            btn.setCursor(Qt.PointingHandCursor)
            tokens.themed(btn, "QPushButton { color: {accent}; padding: 0 4px; }QPushButton:hover { text-decoration: underline; }")
            btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self._select_all_btn.clicked.connect(lambda: self._set_all(Qt.Checked))
        self._deselect_all_btn.clicked.connect(
            lambda: self._set_all(Qt.Unchecked)
        )

        verbs = QHBoxLayout()
        verbs.setContentsMargins(0, 0, 0, 0)
        verbs.setSpacing(8)
        verbs.addWidget(self._select_all_btn)
        verbs.addWidget(self._deselect_all_btn)
        verbs.addStretch(1)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemChanged.connect(self._on_item_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(title_label)
        layout.addWidget(self._search)
        layout.addLayout(verbs)
        layout.addWidget(self._tree, stretch=1)

        self._build(rows)

    # ── population ──

    def _build(self, rows: list[tuple[int, Optional[int], str]]) -> None:
        self._tree.blockSignals(True)
        try:
            self._tree.clear()
            self._items.clear()
            present = {rid for rid, _, _ in rows}
            # Two passes: make every item, then parent them. This tolerates a
            # parent appearing after its child in the row list.
            for rid, _parent_id, label in rows:
                item = QTreeWidgetItem([label])
                item.setData(0, Qt.UserRole, int(rid))
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked)
                self._items[rid] = item
            for rid, parent_id, _label in rows:
                item = self._items[rid]
                if parent_id is not None and parent_id in present:
                    self._items[parent_id].addChild(item)
                else:
                    self._tree.addTopLevelItem(item)
            # Parents with children become tri-state (auto partial/checked).
            for item in self._items.values():
                if item.childCount():
                    item.setFlags(item.flags() | Qt.ItemIsAutoTristate)
            self._tree.expandAll()
        finally:
            self._tree.blockSignals(False)

    # ── checked-state API ──

    def checked_ids(self) -> list[int]:
        """Ids of fully-checked rows (Qt.Checked). A partially-checked parent
        is excluded; its checked descendants appear in their own right."""
        out: list[int] = []
        it = QTreeWidgetItemIterator(self._tree)
        while it.value():
            item = it.value()
            if item.checkState(0) == Qt.Checked:
                out.append(int(item.data(0, Qt.UserRole)))
            it += 1
        return out

    def all_ids(self) -> list[int]:
        return list(self._all_ids)

    def is_all_checked(self) -> bool:
        return set(self.checked_ids()) == set(self._all_ids)

    def set_checked_ids(self, ids: Optional[Iterable[int]]) -> None:
        """Set the checked subset. ``None`` / empty means "all checked" (the
        saved-filter convention where empty == all). Parent tri-states are
        recomputed bottom-up afterwards."""
        target: Optional[set[int]] = set(ids) if ids else None
        self._tree.blockSignals(True)
        try:
            for rid, item in self._items.items():
                state = (
                    Qt.Checked if (target is None or rid in target)
                    else Qt.Unchecked
                )
                item.setCheckState(0, state)
            # Auto-tristate only rolls leaf changes up when they arrive through
            # signals; we set the boxes silently above, so reconcile every
            # parent explicitly from its children.
            self._reconcile_all_parents()
        finally:
            self._tree.blockSignals(False)
        self.changed.emit()

    # ── handlers ──

    def _set_all(self, state: Qt.CheckState) -> None:
        # Bulk-toggle the visible subset (search-scoped, like CheckListPanel).
        self._tree.blockSignals(True)
        try:
            it = QTreeWidgetItemIterator(self._tree)
            while it.value():
                item = it.value()
                if not item.isHidden():
                    item.setCheckState(0, state)
                it += 1
            self._reconcile_all_parents()
        finally:
            self._tree.blockSignals(False)
        self.changed.emit()

    def _on_item_changed(self, item: QTreeWidgetItem, _col: int) -> None:
        # Cascade a user click down to descendants, then let auto-tristate roll
        # the change back up. Guard re-entrancy with blockSignals.
        self._tree.blockSignals(True)
        try:
            state = item.checkState(0)
            if state != Qt.PartiallyChecked:
                self._set_subtree(item, state)
        finally:
            self._tree.blockSignals(False)
        self.changed.emit()

    # ── helpers ──

    def _set_subtree(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, state)
            self._set_subtree(child, state)

    def _reconcile_all_parents(self) -> None:
        for i in range(self._tree.topLevelItemCount()):
            self._reconcile(self._tree.topLevelItem(i))

    def _reconcile(self, item: QTreeWidgetItem) -> Qt.CheckState:
        """Post-order: set each parent's state from its children's."""
        if item.childCount() == 0:
            return item.checkState(0)
        states = [self._reconcile(item.child(i)) for i in range(item.childCount())]
        if all(s == Qt.Checked for s in states):
            new = Qt.Checked
        elif all(s == Qt.Unchecked for s in states):
            new = Qt.Unchecked
        else:
            new = Qt.PartiallyChecked
        item.setCheckState(0, new)
        return new

    def _on_search_text_changed(self, text: str) -> None:
        needle = text.strip().lower()
        if not needle:
            it = QTreeWidgetItemIterator(self._tree)
            while it.value():
                it.value().setHidden(False)
                it += 1
            return
        # A row is visible if it matches, or any ancestor/descendant matches.
        self._apply_search(needle)

    def _apply_search(self, needle: str) -> None:
        def visit(item: QTreeWidgetItem, ancestor_match: bool) -> bool:
            self_match = needle in item.text(0).lower()
            descendant_match = False
            for i in range(item.childCount()):
                if visit(item.child(i), ancestor_match or self_match):
                    descendant_match = True
            visible = self_match or ancestor_match or descendant_match
            item.setHidden(not visible)
            return self_match or descendant_match

        for i in range(self._tree.topLevelItemCount()):
            visit(self._tree.topLevelItem(i), False)
