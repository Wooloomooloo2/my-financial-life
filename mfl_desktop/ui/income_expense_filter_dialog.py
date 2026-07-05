"""Filter dialog for the Income & Expense report (ADR-064 / Arc E, E1).

Modal editor opened by the report window's "Filter…" button. Houses the
report's filter dimensions:

- Period preset (with Custom range pickers)
- Granularity (auto / weekly / monthly / quarterly / annually)
- Accounts — a search-enabled checklist (:class:`CheckListPanel`)
- Categories — a search-enabled checklist over the income + expense
  categories (ADR-088 amend). Empty == all; a picked parent expands to its
  descendants in the window before the query runs.

Returns the chosen :class:`IncomeExpenseFilters` on Accepted via
:py:meth:`values`. Income vs expense is still decided by category kind in
SQL — the category checklist only narrows *which* income/expense categories
feed the totals (there's no rollup; the report has no per-category breakdown
in E1). The display currency is a top-bar view preference, not a saved
filter, so it lives on the window, not here.

The period/granularity/accounts plumbing + the OK/Cancel scaffold come
from :class:`ReportFilterDialogBase` (ADR-084).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, CategoryNode, Repository
from mfl_desktop.reports import category_path
from mfl_desktop.reports.filters import (
    SPENDING_PERIOD_KEYS, IncomeExpenseFilters,
)
from mfl_desktop.ui.check_list_panel import CheckListPanel
from mfl_desktop.ui.report_filter_dialog_base import ReportFilterDialogBase


class IncomeExpenseFilterDialog(ReportFilterDialogBase):
    """Modal filter editor — single trip in / out, accept commits."""

    def __init__(
        self,
        repo: Repository,
        *,
        current: IncomeExpenseFilters,
        accounts: list[AccountSummary],
        categories: list[CategoryNode],
        parent=None,
    ) -> None:
        super().__init__(parent, title="Filter — Income & Expense")
        self.resize(720, 580)

        self._repo = repo
        self._current = current
        self._all_accounts = accounts
        self._all_categories = categories

        # ── period + granularity ──
        period_combo = self._make_period_combo(
            SPENDING_PERIOD_KEYS, current.period_key,
        )
        custom_from, custom_to = self._make_custom_dates(
            current.period_key, current.custom_start, current.custom_end,
        )
        granularity_combo = self._make_granularity_combo(current.granularity)

        # Transfers between own accounts aren't income or expense; off by
        # default (ADR-064). Tooltip explains the kind-vs-transfer_id nuance.
        transfers_check = self._make_transfers_check(current.include_transfers)

        period_box = QGroupBox("Period")
        period_form = QFormLayout(period_box)
        period_form.addRow("Preset:", period_combo)
        period_form.addRow("From:", custom_from)
        period_form.addRow("To:", custom_to)
        period_form.addRow("Granularity:", granularity_combo)
        period_form.addRow(transfers_check)

        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(period_box)
        left_layout.addStretch(1)

        # ── accounts + categories checklists ──
        accounts_panel = self._make_accounts_panel(accounts, current.account_ids)

        self._categories_panel = CheckListPanel(
            "Categories",
            self._category_rows(),
            placeholder="Search categories…",
        )
        self._categories_panel.set_checked_ids(current.category_ids or None)

        # ADR-140: which transfer categories to fold in (empty == all). Only
        # meaningful while "Include transfers" is ticked, so it enables/disables
        # with that checkbox.
        self._transfer_categories_panel = CheckListPanel(
            "Transfer categories (empty = all)",
            self._transfer_category_rows(),
            placeholder="Search transfers…",
        )
        self._transfer_categories_panel.set_checked_ids(
            current.transfer_category_ids or None
        )
        self._transfer_categories_panel.setEnabled(current.include_transfers)
        self._transfer_categories_panel.setToolTip(
            "Transfers filed under the ticked categories are counted as cash "
            "flows — an outflow on the Expense side, an inflow on the Income "
            "side. Tick none to include every transfer. Scope to your "
            "operating account(s) so only your side of each transfer counts."
        )
        self._include_transfers_check.toggled.connect(
            self._transfer_categories_panel.setEnabled
        )

        lists_splitter = QSplitter(Qt.Horizontal)
        lists_splitter.addWidget(accounts_panel)
        lists_splitter.addWidget(self._categories_panel)
        lists_splitter.addWidget(self._transfer_categories_panel)
        lists_splitter.setStretchFactor(0, 1)
        lists_splitter.setStretchFactor(1, 1)
        lists_splitter.setStretchFactor(2, 1)
        lists_splitter.setSizes([200, 240, 240])

        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.addWidget(left_column)
        top_splitter.addWidget(lists_splitter)
        top_splitter.setStretchFactor(0, 0)
        top_splitter.setStretchFactor(1, 1)
        top_splitter.setSizes([240, 460])

        self._finalise(top_splitter)
        self._sync_custom_visibility()

    # ── internals ──

    def _category_rows(self) -> list[tuple[int, str]]:
        """``(id, full_path_label)`` rows for every income/expense category
        (ADR-088 amend). Transfer categories are excluded — they're never
        income or expense, so filtering by them would be meaningless. Sorted
        by full breadcrumb (ADR-031) so siblings cluster; selecting a parent
        pulls in its children (the window expands to descendants)."""
        by_id = {c.id: c for c in self._all_categories}
        rows = [
            (c.id, category_path(by_id, c.id))
            for c in self._all_categories
            if c.kind in ("income", "expense")
        ]
        rows.sort(key=lambda pair: pair[1].lower())
        return rows

    def _transfer_category_rows(self) -> list[tuple[int, str]]:
        """``(id, full_path_label)`` rows for every ``kind='transfer'``
        category (ADR-140) — the ones foldable into the report as cash flows.
        Sorted by breadcrumb so siblings cluster."""
        by_id = {c.id: c for c in self._all_categories}
        rows = [
            (c.id, category_path(by_id, c.id))
            for c in self._all_categories
            if c.kind == "transfer"
        ]
        rows.sort(key=lambda pair: pair[1].lower())
        return rows

    def _on_accept(self) -> None:
        period_key, custom_start, custom_end = self._period_and_custom("1y")

        self._result = IncomeExpenseFilters(
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            granularity=self._granularity_combo.currentData() or "auto",
            account_ids=tuple(self._checked_or_all(self._accounts_panel)),
            category_ids=tuple(self._checked_or_all(self._categories_panel)),
            include_transfers=self._include_transfers_check.isChecked(),
            transfer_category_ids=(
                tuple(self._transfer_categories_panel.checked_ids())
                if self._include_transfers_check.isChecked() else ()
            ),
        )
        self.accept()
