"""Account + category filter dialog for the Sankey report (ADR-056 follow-up).

A modal opened by the report window's "Filter…" button. Two checklists side by
side: Accounts (a flat :class:`CheckListPanel`) and Categories (a hierarchical
:class:`CategoryTreePanel`, so a whole income/expense subtree toggles in one
click). Only income/expense categories are offered — the report excludes
transfers, so filtering them would do nothing.

Returns the chosen ``(account_ids, category_ids)`` on Accepted via
:py:meth:`values`; each is an empty tuple when "all" is selected, matching the
saved-filter convention. The dialog is seeded from the current selection so the
user tweaks rather than starts over.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QSplitter,
    QVBoxLayout,
)

from mfl_desktop.db.repository import AccountSummary, CategoryNode, Repository
from mfl_desktop.ui.category_tree_panel import CategoryTreePanel
from mfl_desktop.ui.check_list_panel import CheckListPanel


class SankeyFilterDialog(QDialog):
    """Modal account/category filter editor — accept commits, cancel discards."""

    def __init__(
        self,
        repo: Repository,
        *,
        accounts: list[AccountSummary],
        categories: list[CategoryNode],
        current_account_ids: tuple[int, ...],
        current_category_ids: tuple[int, ...],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Filter — Sankey")
        self.setModal(True)
        self.resize(720, 560)

        self._result: Optional[tuple[tuple[int, ...], tuple[int, ...]]] = None

        self._accounts_panel = CheckListPanel(
            "Accounts",
            [(a.id, a.name) for a in accounts],
            placeholder="Search accounts…",
        )
        self._accounts_panel.set_checked_ids(current_account_ids or None)

        # Only income/expense categories are relevant (transfers are excluded
        # from the report). Drop an ancestor only when it isn't itself
        # income/expense — the kind-cascade rule (ADR-014) means a relevant
        # category's ancestors are normally relevant too, so subtrees stay
        # connected; any stray orphan just floats up to a root.
        relevant = [c for c in categories if c.kind in ("income", "expense")]
        rows = [(c.id, c.parent_id, c.name) for c in relevant]
        self._categories_panel = CategoryTreePanel(
            "Categories",
            rows,
            placeholder="Search categories…",
        )
        self._categories_panel.set_checked_ids(current_category_ids or None)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._accounts_panel)
        splitter.addWidget(self._categories_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([240, 440])

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(splitter, stretch=1)
        root.addWidget(buttons)

    # ── public API ──

    def values(self) -> Optional[tuple[tuple[int, ...], tuple[int, ...]]]:
        return self._result

    # ── internals ──

    def _on_accept(self) -> None:
        accounts = self._accounts_panel.checked_ids()
        if self._accounts_panel.is_all_checked():
            accounts = []
        categories = self._categories_panel.checked_ids()
        if self._categories_panel.is_all_checked():
            categories = []
        self._result = (tuple(accounts), tuple(categories))
        self.accept()
