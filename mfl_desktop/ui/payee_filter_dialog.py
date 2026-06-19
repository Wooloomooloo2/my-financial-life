"""Filter dialog for the Payee report (ADR-066 / Arc E, E2).

Modal editor opened by the report window's "Filter…" button. Houses the
report's filter dimensions:

- Period preset (with Custom range pickers)
- "Show top N payees" (the rest fold into a single "Other" row; 0 = all)
- "Include transfers" toggle (default off — same as Income & Expense)
- Accounts — a search-enabled checklist (:class:`CheckListPanel`)

Returns the chosen :class:`PayeeReportFilters` on Accepted via
:py:meth:`values`. Spending is defined as strict outflow on expense-kind
categories in SQL, and aliases roll up to canonical payees, so there's no
category / payee / kind control here. The display currency is a top-bar
view preference, not a saved filter, so it lives on the window.

The period/top-N/transfers/accounts plumbing + the OK/Cancel scaffold come
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
    SPENDING_PERIOD_KEYS, PayeeReportFilters,
)
from mfl_desktop.ui.report_filter_dialog_base import ReportFilterDialogBase

_TOP_N_TOOLTIP = (
    "Show this many payees ranked by spend; the rest fold into a\n"
    "single 'Other' row. Set to 0 (All payees) to show every payee."
)


class PayeeFilterDialog(ReportFilterDialogBase):
    """Modal filter editor — single trip in / out, accept commits."""

    def __init__(
        self,
        repo: Repository,
        *,
        current: PayeeReportFilters,
        accounts: list[AccountSummary],
        parent=None,
    ) -> None:
        super().__init__(parent, title="Filter — Payee")
        self.resize(520, 560)

        self._repo = repo
        self._current = current
        self._all_accounts = accounts

        # ── period + top-N + transfers ──
        period_combo = self._make_period_combo(
            SPENDING_PERIOD_KEYS, current.period_key,
        )
        custom_from, custom_to = self._make_custom_dates(
            current.period_key, current.custom_start, current.custom_end,
        )
        top_n = self._make_top_n_spin(
            max(0, current.top_n),
            special_value_text="All payees",
            tooltip=_TOP_N_TOOLTIP,
        )
        transfers_check = self._make_transfers_check(current.include_transfers)

        period_box = QGroupBox("Period & display")
        period_form = QFormLayout(period_box)
        period_form.addRow("Preset:", period_combo)
        period_form.addRow("From:", custom_from)
        period_form.addRow("To:", custom_to)
        period_form.addRow("Show top:", top_n)
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

        self._result = PayeeReportFilters(
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            account_ids=tuple(self._checked_or_all(self._accounts_panel)),
            top_n=self._top_n.value(),
            include_transfers=self._include_transfers_check.isChecked(),
        )
        self.accept()
