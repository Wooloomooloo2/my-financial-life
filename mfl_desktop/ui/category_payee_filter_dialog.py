"""Filter dialog for the Category & Payee report (ADR-068 / Arc E, E3).

Modal editor opened by the report window's "Filter…" button. Houses the
period, the per-level "Show top N" cap, the transfers toggle, and the
accounts checklist. The primary dimension (Category vs Payee) is a top-bar
toggle on the window, not here, and the display currency is a view
preference — so neither appears in this dialog. Returns the chosen
:class:`CategoryPayeeFilters` (preserving the current ``group_by``).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QSpinBox,
    QVBoxLayout,
)

from mfl_desktop.account_summary import period_bounds
from mfl_desktop.ui.date_widgets import make_date_edit, make_period_combo
from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.reports.filters import (
    SPENDING_PERIOD_KEYS, CategoryPayeeFilters,
)
from mfl_desktop.ui.check_list_panel import CheckListPanel

# Period labels reuse account_summary.PERIOD_LABELS (ADR-082, single source).


class CategoryPayeeFilterDialog(QDialog):
    """Modal filter editor — single trip in / out, accept commits."""

    def __init__(
        self,
        repo: Repository,
        *,
        current: CategoryPayeeFilters,
        accounts: list[AccountSummary],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Filter — Category & Payee")
        self.setModal(True)
        self.resize(520, 560)

        self._repo = repo
        self._current = current
        self._all_accounts = accounts
        self._result: Optional[CategoryPayeeFilters] = None

        # ── period ──
        self._period_combo = make_period_combo(
            SPENDING_PERIOD_KEYS, current=current.period_key,
        )
        self._period_combo.currentIndexChanged.connect(
            self._sync_custom_visibility,
        )

        cf, ct = self._initial_custom_dates(current)
        self._custom_from = make_date_edit(QDate(cf.year, cf.month, cf.day))
        self._custom_to = make_date_edit(QDate(ct.year, ct.month, ct.day))

        # ── top-N + transfers ──
        self._top_n = QSpinBox()
        self._top_n.setRange(0, 200)
        self._top_n.setValue(max(0, current.top_n))
        self._top_n.setSpecialValueText("All")  # shown when value == 0
        self._top_n.setToolTip(
            "Show at most this many rows at each level; the rest are\n"
            "omitted (with a hidden-count note). 0 (All) shows everything."
        )

        self._include_transfers_check = QCheckBox("Include transfers")
        self._include_transfers_check.setChecked(current.include_transfers)
        self._include_transfers_check.setToolTip(
            "Transfers between your own accounts are excluded by default.\n"
            "Categories marked 'transfer' are always excluded; this also\n"
            "drops linked transfer pairs filed under other categories."
        )

        period_box = QGroupBox("Period & display")
        period_form = QFormLayout(period_box)
        period_form.addRow("Preset:", self._period_combo)
        period_form.addRow("From:", self._custom_from)
        period_form.addRow("To:", self._custom_to)
        period_form.addRow("Show top:", self._top_n)
        period_form.addRow(self._include_transfers_check)

        # ── accounts ──
        self._accounts_panel = CheckListPanel(
            "Accounts",
            [(a.id, a.name) for a in accounts],
            placeholder="Search accounts…",
        )
        self._accounts_panel.set_checked_ids(current.account_ids or None)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(period_box)
        root.addWidget(self._accounts_panel, stretch=1)
        root.addWidget(buttons)

        self._sync_custom_visibility()

    # ── public API ──

    def values(self) -> Optional[CategoryPayeeFilters]:
        return self._result

    # ── internals ──

    def _sync_custom_visibility(self) -> None:
        is_custom = self._period_combo.currentData() == "custom"
        self._custom_from.setEnabled(is_custom)
        self._custom_to.setEnabled(is_custom)

    def _on_accept(self) -> None:
        period_key = self._period_combo.currentData() or "1y"
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

        # group_by is owned by the window's toggle — preserve it.
        self._result = CategoryPayeeFilters(
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            account_ids=tuple(accounts),
            group_by=self._current.group_by,
            top_n=self._top_n.value(),
            include_transfers=self._include_transfers_check.isChecked(),
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
    def _initial_custom_dates(f: CategoryPayeeFilters) -> tuple[date, date]:
        today = date.today()
        if f.period_key == "custom" and f.custom_start and f.custom_end:
            try:
                return (
                    date.fromisoformat(f.custom_start),
                    date.fromisoformat(f.custom_end),
                )
            except ValueError:
                pass
        try:
            return period_bounds(f.period_key, today)
        except ValueError:
            return (today.replace(day=1), today)
