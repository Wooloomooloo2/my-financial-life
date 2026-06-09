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
:py:meth:`values`. Mirrors SpendingFilterDialog's shape (ADR-039) and reuses
:class:`CheckListPanel`.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
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

_PERIOD_LABELS: dict[str, str] = {
    "ytd":    "Year to date",
    "1y":     "Last 12 months",
    "3y":     "Last 3 years",
    "5y":     "Last 5 years",
    "max":    "Max (all history)",
    "custom": "Custom",
}


def security_label(s: SecurityRow) -> str:
    """Checklist label for a security: ``TSLA · Tesla Inc`` when a ticker is
    on file, otherwise just the name."""
    sym = (s.symbol or "").strip()
    return f"{sym} · {s.name}" if sym else s.name


class InvestmentReturnsFilterDialog(QDialog):
    """Modal filter editor — single trip in / out, accept commits."""

    def __init__(
        self,
        repo: Repository,
        *,
        current: InvestmentReturnsFilters,
        accounts: list[AccountSummary],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Filter — Investment Returns")
        self.setModal(True)
        self.resize(720, 560)

        self._repo = repo
        self._current = current
        self._all_accounts = accounts
        self._result: Optional[InvestmentReturnsFilters] = None

        # ── period ──
        self._period_combo = QComboBox()
        for key in INVESTMENT_RETURNS_PERIOD_KEYS:
            self._period_combo.addItem(_PERIOD_LABELS[key], userData=key)
        self._set_combo_to(self._period_combo, current.period_key)
        self._period_combo.currentIndexChanged.connect(self._sync_custom_visibility)

        cf, ct = self._initial_custom_dates(current)
        self._custom_from = QDateEdit(QDate(cf.year, cf.month, cf.day))
        self._custom_from.setCalendarPopup(True)
        self._custom_from.setDisplayFormat("yyyy-MM-dd")
        self._custom_to = QDateEdit(QDate(ct.year, ct.month, ct.day))
        self._custom_to.setCalendarPopup(True)
        self._custom_to.setDisplayFormat("yyyy-MM-dd")

        period_box = QGroupBox("Period")
        period_form = QFormLayout(period_box)
        period_form.addRow("Preset:", self._period_combo)
        period_form.addRow("From:", self._custom_from)
        period_form.addRow("To:", self._custom_to)

        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(period_box)
        left_layout.addStretch(1)

        # ── checklists ──
        self._accounts_panel = CheckListPanel(
            "Accounts",
            [(a.id, a.name) for a in accounts],
            placeholder="Search accounts…",
        )
        self._accounts_panel.set_checked_ids(current.account_ids or None)
        self._accounts_panel.changed.connect(self._on_accounts_changed)

        self._securities_panel = CheckListPanel(
            "Securities",
            self._security_rows_for(self._selected_account_ids()),
            placeholder="Search securities…",
        )
        self._securities_panel.set_checked_ids(current.security_ids or None)

        lists_splitter = QSplitter(Qt.Horizontal)
        lists_splitter.addWidget(self._accounts_panel)
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

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(top_splitter, stretch=1)
        root.addWidget(buttons)

        self._sync_custom_visibility()

    # ── public API ──

    def values(self) -> Optional[InvestmentReturnsFilters]:
        return self._result

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

    def _sync_custom_visibility(self) -> None:
        is_custom = self._period_combo.currentData() == "custom"
        self._custom_from.setEnabled(is_custom)
        self._custom_to.setEnabled(is_custom)

    def _on_accept(self) -> None:
        period_key = self._period_combo.currentData() or "max"
        custom_start: Optional[str] = None
        custom_end: Optional[str] = None
        if period_key == "custom":
            cf = self._custom_from.date()
            ct = self._custom_to.date()
            if cf > ct:
                cf, ct = ct, cf
            custom_start = cf.toString(Qt.ISODate)
            custom_end = ct.toString(Qt.ISODate)

        accounts = self._accounts_panel.checked_ids()
        if self._accounts_panel.is_all_checked():
            accounts = []
        securities = self._securities_panel.checked_ids()
        if self._securities_panel.is_all_checked():
            securities = []

        self._result = InvestmentReturnsFilters(
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            account_ids=tuple(accounts),
            security_ids=tuple(securities),
        )
        self.accept()

    # ── helpers ──

    @staticmethod
    def _set_combo_to(combo: QComboBox, value: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    @staticmethod
    def _initial_custom_dates(
        f: InvestmentReturnsFilters,
    ) -> tuple[date, date]:
        today = date.today()
        if f.period_key == "custom" and f.custom_start and f.custom_end:
            try:
                return (
                    date.fromisoformat(f.custom_start),
                    date.fromisoformat(f.custom_end),
                )
            except ValueError:
                pass
        return today.replace(month=1, day=1), today