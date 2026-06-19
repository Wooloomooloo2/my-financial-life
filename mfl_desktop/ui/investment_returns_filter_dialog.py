"""Filter dialog for the Investment Returns report (ADR-046).

Modal editor opened by the report window's "Filter…" button. Houses the
three dimensions the report supports:

- Period preset (YTD / 1Y / 3Y / 5Y / Max / Custom, with date pickers for
  Custom). "Max" = lifetime (first transaction → today).
- Accounts — a search-enabled checklist of investment accounts. No
  selection = the whole portfolio.
- Securities — a search-enabled checklist of the securities held in the
  selected accounts. No selection = every security. The list re-queries
  when the account selection changes so it only ever offers relevant
  securities (preserving the previously-checked subset where it carries
  over).

Returns the chosen :class:`InvestmentReturnsFilters` on Accepted via
:py:meth:`values`. The period/accounts plumbing + the OK/Cancel scaffold
come from :class:`ReportFilterDialogBase` (ADR-084); the securities panel
and its account-driven rebuild are this report's specials.
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

from mfl_desktop.db.repository import AccountSummary, Repository, SecurityRow
from mfl_desktop.reports.filters import (
    INVESTMENT_RETURNS_PERIOD_KEYS, InvestmentReturnsFilters,
)
from mfl_desktop.ui.check_list_panel import CheckListPanel
from mfl_desktop.ui.report_filter_dialog_base import ReportFilterDialogBase


def security_label(s: SecurityRow) -> str:
    """Checklist label for a security: ``TSLA · Tesla Inc`` when a ticker is
    on file, otherwise just the name."""
    sym = (s.symbol or "").strip()
    return f"{sym} · {s.name}" if sym else s.name


class InvestmentReturnsFilterDialog(ReportFilterDialogBase):
    """Modal filter editor — single trip in / out, accept commits."""

    def __init__(
        self,
        repo: Repository,
        *,
        current: InvestmentReturnsFilters,
        accounts: list[AccountSummary],
        parent=None,
    ) -> None:
        super().__init__(parent, title="Filter — Investment Returns")
        self.resize(720, 560)

        self._repo = repo
        self._current = current
        self._all_accounts = accounts

        # ── period ──
        period_combo = self._make_period_combo(
            INVESTMENT_RETURNS_PERIOD_KEYS, current.period_key,
        )
        custom_from, custom_to = self._make_custom_dates(
            current.period_key, current.custom_start, current.custom_end,
        )

        period_box = QGroupBox("Period")
        period_form = QFormLayout(period_box)
        period_form.addRow("Preset:", period_combo)
        period_form.addRow("From:", custom_from)
        period_form.addRow("To:", custom_to)

        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(period_box)
        left_layout.addStretch(1)

        # ── checklists ──
        accounts_panel = self._make_accounts_panel(accounts, current.account_ids)
        accounts_panel.changed.connect(self._on_accounts_changed)

        self._securities_panel = CheckListPanel(
            "Securities",
            self._security_rows_for(self._selected_account_ids()),
            placeholder="Search securities…",
        )
        self._securities_panel.set_checked_ids(current.security_ids or None)

        lists_splitter = QSplitter(Qt.Horizontal)
        lists_splitter.addWidget(accounts_panel)
        lists_splitter.addWidget(self._securities_panel)
        lists_splitter.setStretchFactor(0, 1)
        lists_splitter.setStretchFactor(1, 1)
        lists_splitter.setSizes([280, 340])

        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.addWidget(left_column)
        top_splitter.addWidget(lists_splitter)
        top_splitter.setStretchFactor(0, 0)
        top_splitter.setStretchFactor(1, 1)
        top_splitter.setSizes([220, 500])

        self._finalise(top_splitter)
        self._sync_custom_visibility()

    # ── internals ──

    def _selected_account_ids(self) -> list[int]:
        """Currently-checked account ids, or every investment account when
        the panel is all-checked (= "whole portfolio")."""
        if self._accounts_panel.is_all_checked():
            return [a.id for a in self._all_accounts]
        return self._accounts_panel.checked_ids()

    def _security_rows_for(self, account_ids: list[int]) -> list[tuple[int, str]]:
        secs = self._repo.list_securities_for_accounts(account_ids)
        return [(s.id, security_label(s)) for s in secs]

    def _on_accounts_changed(self) -> None:
        """Re-query the securities list against the new account selection,
        preserving any checked securities that survive the change."""
        prior_checked = set(self._securities_panel.checked_ids())
        new_rows = self._security_rows_for(self._selected_account_ids())
        new_ids = {rid for rid, _ in new_rows}
        carryover = prior_checked & new_ids
        self._securities_panel.replace_rows(new_rows)
        if carryover and carryover != new_ids:
            self._securities_panel.set_checked_ids(carryover)

    def _on_accept(self) -> None:
        period_key, custom_start, custom_end = self._period_and_custom("max")

        self._result = InvestmentReturnsFilters(
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            account_ids=tuple(self._checked_or_all(self._accounts_panel)),
            security_ids=tuple(self._checked_or_all(self._securities_panel)),
        )
        self.accept()
