"""Filter dialog for the Investment Income view (ADR-108).

Modal editor opened by the window's "Filter…" button. Three dimensions:

- Period preset (YTD / 1Y / 3Y / 5Y / Max / Custom). Default 1Y = trailing
  twelve months (TTM), the income-investing convention.
- Accounts — a search-enabled checklist of investment accounts. No selection
  = the whole portfolio.
- Include reinvested dividends — a checkbox (default on) mirroring ADR-089:
  count DRIP / reinvested distributions (valued at quantity × price) as income
  alongside cash dividends, coupons and interest.

The period/accounts plumbing + the OK/Cancel scaffold come from
:class:`ReportFilterDialogBase` (ADR-084). Returns the chosen
:class:`IncomeFilters` on Accepted via :py:meth:`values`.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.periods import INVESTMENT_PRESETS
from mfl_desktop.reports.investment_income import IncomeFilters
from mfl_desktop.ui.report_filter_dialog_base import ReportFilterDialogBase


class InvestmentIncomeFilterDialog(ReportFilterDialogBase):
    """Modal filter editor — single trip in / out, accept commits."""

    def __init__(
        self,
        repo: Repository,
        *,
        current: IncomeFilters,
        accounts: list[AccountSummary],
        parent=None,
    ) -> None:
        super().__init__(parent, title="Filter — Investment Income")
        self.resize(640, 520)

        self._repo = repo
        self._current = current
        self._all_accounts = accounts

        # ── period ──
        period_combo = self._make_period_combo(
            INVESTMENT_PRESETS, current.period_key,
        )
        custom_from, custom_to = self._make_custom_dates(
            current.period_key, current.custom_start, current.custom_end,
        )
        period_box = QGroupBox("Period")
        period_form = QFormLayout(period_box)
        period_form.addRow("Preset:", period_combo)
        period_form.addRow("From:", custom_from)
        period_form.addRow("To:", custom_to)

        # ── reinvest toggle (ADR-089 parity) ──
        self._reinvest_check = QCheckBox("Include reinvested dividends")
        self._reinvest_check.setChecked(current.include_reinvested)
        self._reinvest_check.setToolTip(
            "Count DRIP / reinvested distributions (valued at quantity × price)\n"
            "as income, alongside cash dividends, coupons and interest."
        )

        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(period_box)
        left_layout.addWidget(self._reinvest_check)
        left_layout.addStretch(1)

        # ── accounts ──
        accounts_panel = self._make_accounts_panel(accounts, current.account_ids)

        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.addWidget(left_column)
        top_splitter.addWidget(accounts_panel)
        top_splitter.setStretchFactor(0, 0)
        top_splitter.setStretchFactor(1, 1)
        top_splitter.setSizes([240, 380])

        self._finalise(top_splitter)
        self._sync_custom_visibility()

    def _on_accept(self) -> None:
        period_key, custom_start, custom_end = self._period_and_custom("1y")
        self._result = IncomeFilters(
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            account_ids=tuple(self._checked_or_all(self._accounts_panel)),
            include_reinvested=self._reinvest_check.isChecked(),
        )
        self.accept()
