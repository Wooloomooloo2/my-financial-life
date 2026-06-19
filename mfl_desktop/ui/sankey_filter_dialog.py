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

This dialog adopts :class:`ReportFilterDialogBase` (ADR-084) **partially** —
it has no period block and returns a tuple rather than a filter dataclass, but
still reuses the base's accounts checklist, all-checked→``[]`` normalisation,
OK/Cancel scaffold, and ``values()``. Its category **tree** stays bespoke.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter

from mfl_desktop.db.repository import AccountSummary, CategoryNode, Repository
from mfl_desktop.ui.category_tree_panel import CategoryTreePanel
from mfl_desktop.ui.report_filter_dialog_base import ReportFilterDialogBase


class SankeyFilterDialog(ReportFilterDialogBase):
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
        super().__init__(parent, title="Filter — Sankey")
        self.resize(720, 560)

        accounts_panel = self._make_accounts_panel(accounts, current_account_ids)

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
        splitter.addWidget(accounts_panel)
        splitter.addWidget(self._categories_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([240, 440])

        self._finalise(splitter)

    # ── public API ──

    def values(self) -> Optional[tuple[tuple[int, ...], tuple[int, ...]]]:
        return self._result

    # ── internals ──

    def _on_accept(self) -> None:
        accounts = self._checked_or_all(self._accounts_panel)
        categories = self._checked_or_all(self._categories_panel)
        self._result = (tuple(accounts), tuple(categories))
        self.accept()
