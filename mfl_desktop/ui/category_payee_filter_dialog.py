"""Filter dialog for the Category & Payee report (ADR-068 / Arc E, E3).

Modal editor opened by the report window's "Filter…" button. Houses the
period, the transfers toggle, and the accounts checklist. The primary
dimension (Category vs Payee), the category rollup level, and the "Show top"
cap are live top-bar controls on the window (ADR-134), not here; the display
currency is likewise a view preference — so none appear in this dialog.
Returns the chosen :class:`CategoryPayeeFilters`, preserving the window-owned
``group_by`` / ``rollup_level`` / ``top_n``.

The period/transfers/accounts plumbing + the OK/Cancel scaffold come from
:class:`ReportFilterDialogBase` (ADR-084).
"""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QVBoxLayout,
)

from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.reports.filters import (
    CATEGORY_PAYEE_PERIOD_KEYS, CategoryPayeeFilters,
)
from mfl_desktop.ui.report_filter_dialog_base import ReportFilterDialogBase


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

        # ── period + transfers ──
        period_combo = self._make_period_combo(
            CATEGORY_PAYEE_PERIOD_KEYS, current.period_key,
        )
        custom_from, custom_to = self._make_custom_dates(
            current.period_key, current.custom_start, current.custom_end,
        )
        transfers_check = self._make_transfers_check(current.include_transfers)

        period_box = QGroupBox("Period")
        period_form = QFormLayout(period_box)
        period_form.addRow("Preset:", period_combo)
        period_form.addRow("From:", custom_from)
        period_form.addRow("To:", custom_to)
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

        # group_by / rollup_level / top_n are owned by the window's top-bar
        # controls — preserve them (replace() keeps every other field too).
        self._result = replace(
            self._current,
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            account_ids=tuple(self._checked_or_all(self._accounts_panel)),
            include_transfers=self._include_transfers_check.isChecked(),
        )
        self.accept()
