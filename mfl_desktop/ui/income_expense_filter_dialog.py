"""Filter dialog for the Income & Expense report (ADR-064 / Arc E, E1).

Modal editor opened by the report window's "Filter…" button. Houses the
report's filter dimensions:

- Period preset (with Custom range pickers)
- Granularity (auto / weekly / monthly / quarterly / annually)
- Accounts — a search-enabled checklist (:class:`CheckListPanel`)

Returns the chosen :class:`IncomeExpenseFilters` on Accepted via
:py:meth:`values`. Income vs expense is decided by category kind in SQL,
so there's no category/kind control here (and no rollup — the report has
no per-category breakdown in E1). The display currency is a top-bar view
preference, not a saved filter, so it lives on the window, not here.

The period/granularity/accounts plumbing + the OK/Cancel scaffold come
from :class:`ReportFilterDialogBase` (ADR-084).
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QVBoxLayout,
)

from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.reports.filters import (
    SPENDING_PERIOD_KEYS, IncomeExpenseFilters,
)
from mfl_desktop.ui.report_filter_dialog_base import ReportFilterDialogBase


class IncomeExpenseFilterDialog(ReportFilterDialogBase):
    """Modal filter editor — single trip in / out, accept commits."""

    def __init__(
        self,
        repo: Repository,
        *,
        current: IncomeExpenseFilters,
        accounts: list[AccountSummary],
        parent=None,
    ) -> None:
        super().__init__(parent, title="Filter — Income & Expense")
        self.resize(520, 560)

        self._repo = repo
        self._current = current
        self._all_accounts = accounts

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

        # ── accounts ──
        accounts_panel = self._make_accounts_panel(accounts, current.account_ids)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        body.addWidget(period_box)
        body.addWidget(accounts_panel, stretch=1)

        self._finalise(body)
        self._sync_custom_visibility()

    # ── internals ──

    def _on_accept(self) -> None:
        period_key, custom_start, custom_end = self._period_and_custom("1y")

        self._result = IncomeExpenseFilters(
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            granularity=self._granularity_combo.currentData() or "auto",
            account_ids=tuple(self._checked_or_all(self._accounts_panel)),
            include_transfers=self._include_transfers_check.isChecked(),
        )
        self.accept()
