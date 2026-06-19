"""Filter dialog for the Category & Payee report (ADR-068 / Arc E, E3).

Modal editor opened by the report window's "Filter…" button. Houses the
period, the per-level "Show top N" cap, the transfers toggle, and the
accounts checklist. The primary dimension (Category vs Payee) is a top-bar
toggle on the window, not here, and the display currency is a view
preference — so neither appears in this dialog. Returns the chosen
:class:`CategoryPayeeFilters` (preserving the current ``group_by``).

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
    SPENDING_PERIOD_KEYS, CategoryPayeeFilters,
)
from mfl_desktop.ui.report_filter_dialog_base import ReportFilterDialogBase

_TOP_N_TOOLTIP = (
    "Show at most this many rows at each level; the rest are\n"
    "omitted (with a hidden-count note). 0 (All) shows everything."
)


class CategoryPayeeFilterDialog(ReportFilterDialogBase):
    """Modal filter editor — single trip in / out, accept commits."""

    def __init__(
        self,
        repo: Repository,
        *,
        current: CategoryPayeeFilters,
        accounts: list[AccountSummary],
        parent=None,
    ) -> None:
        super().__init__(parent, title="Filter — Category & Payee")
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
            special_value_text="All",
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

        # group_by is owned by the window's toggle — preserve it.
        self._result = CategoryPayeeFilters(
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            account_ids=tuple(self._checked_or_all(self._accounts_panel)),
            group_by=self._current.group_by,
            top_n=self._top_n.value(),
            include_transfers=self._include_transfers_check.isChecked(),
        )
        self.accept()
