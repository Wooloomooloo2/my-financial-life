"""Account + category filter dialog for the Sankey report (ADR-056 follow-up).

A modal opened by the report window's "Filter…" button. Two checklists side by
side: Accounts (a flat :class:`CheckListPanel`) and Categories (a hierarchical
:class:`CategoryTreePanel`, so a whole income/expense subtree toggles in one
click). An "Include transfers" checkbox (ADR-146) folds transfer legs into the
diagram as directional cash flows; when ticked, a third checklist lets the user
pick which transfer categories to fold in (empty == all).

Returns the chosen ``(account_ids, category_ids, include_transfers,
transfer_category_ids)`` on Accepted via :py:meth:`values`; the id tuples are
empty when "all" is selected, matching the saved-filter convention. The dialog
is seeded from the current selection so the user tweaks rather than starts over.

This dialog adopts :class:`ReportFilterDialogBase` (ADR-084) **partially** —
it has no period block and returns a tuple rather than a filter dataclass, but
still reuses the base's accounts checklist, all-checked→``[]`` normalisation,
"Include transfers" checkbox, OK/Cancel scaffold, and ``values()``. Its
category **tree** stays bespoke.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

from mfl_desktop.db.repository import AccountSummary, CategoryNode, Repository
from mfl_desktop.reports import category_path
from mfl_desktop.ui.category_tree_panel import CategoryTreePanel
from mfl_desktop.ui.check_list_panel import CheckListPanel
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
        current_include_transfers: bool = False,
        current_transfer_category_ids: tuple[int, ...] = (),
        parent=None,
    ) -> None:
        super().__init__(parent, title="Filter — Cash Flow")
        self.resize(860, 560)

        self._all_categories = categories

        accounts_panel = self._make_accounts_panel(accounts, current_account_ids)

        # Only income/expense categories are relevant here (transfers get their
        # own picker below). Drop an ancestor only when it isn't itself
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

        # ADR-146: fold transfer legs in as directional cash flows, and pick
        # which transfer categories (empty == all). The picker is meaningful
        # only while "Include transfers" is ticked, so it enables with it.
        transfers_check = self._make_transfers_check(
            current_include_transfers, text="Include transfers"
        )
        self._transfer_categories_panel = CheckListPanel(
            "Transfer categories (empty = all)",
            self._transfer_category_rows(),
            placeholder="Search transfers…",
        )
        self._transfer_categories_panel.set_checked_ids(
            current_transfer_category_ids or None
        )
        self._transfer_categories_panel.setEnabled(current_include_transfers)
        self._transfer_categories_panel.setToolTip(
            "Transfers filed under the ticked categories are folded in as cash "
            "flows — an outflow on the Expense side, an inflow on the Income "
            "side. Tick none to include every transfer. A transfer counts on "
            "both sides only if both its accounts are in scope; scope to one "
            "account (above) to see just that side's leg."
        )
        self._include_transfers_check.toggled.connect(
            self._transfer_categories_panel.setEnabled
        )

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(accounts_panel)
        splitter.addWidget(self._categories_panel)
        splitter.addWidget(self._transfer_categories_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([220, 400, 240])

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(transfers_check)
        layout.addWidget(splitter, 1)

        self._finalise(container)

    # ── public API ──

    def values(
        self,
    ) -> Optional[tuple[tuple[int, ...], tuple[int, ...], bool, tuple[int, ...]]]:
        return self._result

    # ── internals ──

    def _transfer_category_rows(self) -> list[tuple[int, str]]:
        """``(id, full_path_label)`` rows for every ``kind='transfer'``
        category (ADR-146 / ADR-140) — the ones foldable into the diagram as
        cash flows. Sorted by breadcrumb so siblings cluster."""
        by_id = {c.id: c for c in self._all_categories}
        rows = [
            (c.id, category_path(by_id, c.id))
            for c in self._all_categories
            if c.kind == "transfer"
        ]
        rows.sort(key=lambda pair: pair[1].lower())
        return rows

    def _on_accept(self) -> None:
        accounts = self._checked_or_all(self._accounts_panel)
        categories = self._checked_or_all(self._categories_panel)
        include_transfers = self._include_transfers_check.isChecked()
        transfer_category_ids = (
            tuple(self._transfer_categories_panel.checked_ids())
            if include_transfers else ()
        )
        self._result = (
            tuple(accounts), tuple(categories),
            include_transfers, transfer_category_ids,
        )
        self.accept()
