"""Filter dialog for the Spending Over Time report (ADR-039 follow-up).

Replaces the always-visible left filter panel with a modal opened by a
"Filter…" button on the report window's top bar. Houses every filter
dimension the report supports:

- Period preset (with Custom range pickers)
- Granularity (auto / weekly / monthly / quarterly / annually)
- Rollup level (top / group / leaf)
- Accounts / Categories / Payees — each a search-enabled checklist
  (:class:`CheckListPanel`) with Select all / Deselect all verbs.
- Include Uncategorised toggle

Returns the chosen :class:`SpendingOverTimeFilters` on Accepted via
:py:meth:`values`. The dialog is initialised from the current filter
state so the user can tweak rather than start from scratch.

The category checklist rebuilds when the rollup level changes — the
distinct bucket-id set shifts (see ADR-030). The widget tries to
preserve the previously-checked subset where the bucket-ids still exist,
otherwise falls back to all-checked.
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
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.account_summary import period_bounds
from mfl_desktop.db.repository import (
    AccountSummary, CategoryNode, Repository,
)
from mfl_desktop.reports import (
    category_group_map, category_path, category_root_map,
)
from mfl_desktop.reports.filters import (
    SPENDING_PERIOD_KEYS, SpendingOverTimeFilters,
)
from mfl_desktop.ui.check_list_panel import CheckListPanel

# Shared with the report window.
UNCATEGORISED_ID = 1

_PERIOD_LABELS: dict[str, str] = {
    "quarter": "Last Quarter",
    "6m":      "Last 6 months",
    "ytd":     "Year to date",
    "1y":      "Last 12 months",
    "3y":      "Last 3 years",
    "custom":  "Custom",
}
_GRANULARITY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Auto",       "auto"),
    ("Weekly",     "weekly"),
    ("Monthly",    "monthly"),
    ("Quarterly",  "quarterly"),
    ("Annually",   "annually"),
)
_ROLLUP_TOP = "top"
_ROLLUP_GROUP = "group"
_ROLLUP_LEAF = "leaf"
_ROLLUP_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Top level", _ROLLUP_TOP),
    ("Group",     _ROLLUP_GROUP),
    ("Leaf",      _ROLLUP_LEAF),
)


class SpendingFilterDialog(QDialog):
    """Modal filter editor — single trip in / out, accept commits."""

    def __init__(
        self,
        repo: Repository,
        *,
        current: SpendingOverTimeFilters,
        accounts: list[AccountSummary],
        categories: list[CategoryNode],
        canonical_payees: list[tuple[int, str]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Filter — Spending Over Time")
        self.setModal(True)
        self.resize(820, 620)

        self._repo = repo
        self._current = current
        self._all_accounts = accounts
        self._all_categories = categories
        self._categories_by_id = {c.id: c for c in categories}
        self._all_canonical_payees = canonical_payees

        # Pre-build the three rollup maps once; the dialog's category
        # checklist swaps between them when the rollup combo changes.
        self._rollup_maps: dict[str, dict[int, int]] = {
            _ROLLUP_TOP:   category_root_map(categories),
            _ROLLUP_GROUP: category_group_map(categories),
            _ROLLUP_LEAF:  {c.id: c.id for c in categories},
        }

        self._result: Optional[SpendingOverTimeFilters] = None

        # ── left column: period + granularity + rollup + uncat toggle ──
        self._period_combo = QComboBox()
        for key in SPENDING_PERIOD_KEYS:
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

        self._granularity_combo = QComboBox()
        for label, value in _GRANULARITY_OPTIONS:
            self._granularity_combo.addItem(label, userData=value)
        self._set_combo_to(self._granularity_combo, current.granularity)

        self._rollup_combo = QComboBox()
        for label, value in _ROLLUP_OPTIONS:
            self._rollup_combo.addItem(label, userData=value)
        self._set_combo_to(self._rollup_combo, current.rollup_level)
        self._rollup_combo.currentIndexChanged.connect(self._on_rollup_changed)

        self._include_uncat_check = QCheckBox("Include Uncategorised")
        self._include_uncat_check.setChecked(current.include_uncategorised)

        period_box = QGroupBox("Period")
        period_form = QFormLayout(period_box)
        period_form.addRow("Preset:", self._period_combo)
        period_form.addRow("From:", self._custom_from)
        period_form.addRow("To:", self._custom_to)

        shape_box = QGroupBox("Shape")
        shape_form = QFormLayout(shape_box)
        shape_form.addRow("Granularity:", self._granularity_combo)
        shape_form.addRow("Rollup:", self._rollup_combo)
        shape_form.addRow(self._include_uncat_check)

        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(period_box)
        left_layout.addWidget(shape_box)
        left_layout.addStretch(1)

        # ── right column: three checklists side-by-side ──
        self._accounts_panel = CheckListPanel(
            "Accounts",
            [(a.id, a.name) for a in accounts],
            placeholder="Search accounts…",
        )
        self._accounts_panel.set_checked_ids(current.account_ids or None)

        category_rows = self._category_rows_for_rollup(current.rollup_level)
        self._categories_panel = CheckListPanel(
            "Categories",
            category_rows,
            placeholder="Search categories…",
        )
        self._categories_panel.set_checked_ids(current.category_ids or None)

        self._payees_panel = CheckListPanel(
            "Payees",
            [(pid, pname) for pid, pname in canonical_payees],
            placeholder="Search payees…",
        )
        self._payees_panel.set_checked_ids(current.payee_ids or None)

        lists_splitter = QSplitter(Qt.Horizontal)
        lists_splitter.addWidget(self._accounts_panel)
        lists_splitter.addWidget(self._categories_panel)
        lists_splitter.addWidget(self._payees_panel)
        lists_splitter.setStretchFactor(0, 1)
        lists_splitter.setStretchFactor(1, 1)
        lists_splitter.setStretchFactor(2, 1)
        lists_splitter.setSizes([200, 260, 200])

        # ── overall layout ──
        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.addWidget(left_column)
        top_splitter.addWidget(lists_splitter)
        top_splitter.setStretchFactor(0, 0)
        top_splitter.setStretchFactor(1, 1)
        top_splitter.setSizes([240, 560])

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

    def values(self) -> Optional[SpendingOverTimeFilters]:
        return self._result

    # ── internals ──

    def _category_rows_for_rollup(self, rollup: str) -> list[tuple[int, str]]:
        """Return ``(id, full_path_label)`` rows for the distinct
        bucket-ids that the rollup map produces over kind='expense'
        categories. Uncategorised is excluded — its own toggle covers it.
        Sorted by full breadcrumb (ADR-031) so siblings cluster."""
        rollup_map = self._rollup_maps[rollup]
        bucket_ids: set[int] = set()
        for c in self._all_categories:
            if c.kind == "expense":
                bucket_ids.add(rollup_map[c.id])
        bucket_ids.discard(UNCATEGORISED_ID)
        rows = [
            (gid, category_path(self._categories_by_id, gid))
            for gid in bucket_ids
            if gid in self._categories_by_id
        ]
        rows.sort(key=lambda pair: pair[1].lower())
        return rows

    def _on_rollup_changed(self, _index: int) -> None:
        rollup = self._rollup_combo.currentData() or _ROLLUP_TOP
        # Try to preserve the previously-checked subset where bucket-ids
        # carry over; otherwise default to all-checked (the natural
        # "fresh start" semantic for a rollup change — see ADR-030).
        prior_checked = set(self._categories_panel.checked_ids())
        new_rows = self._category_rows_for_rollup(rollup)
        new_ids = {rid for rid, _ in new_rows}
        carryover = prior_checked & new_ids
        self._categories_panel.replace_rows(new_rows)
        if carryover and carryover != new_ids:
            self._categories_panel.set_checked_ids(carryover)

    def _sync_custom_visibility(self) -> None:
        is_custom = self._period_combo.currentData() == "custom"
        self._custom_from.setEnabled(is_custom)
        self._custom_to.setEnabled(is_custom)

    def _on_accept(self) -> None:
        period_key = self._period_combo.currentData() or "quarter"
        custom_start: Optional[str] = None
        custom_end: Optional[str] = None
        if period_key == "custom":
            cf = self._custom_from.date()
            ct = self._custom_to.date()
            if cf > ct:
                # Swap silently — the alternative is a modal warning the
                # user has to dismiss every time they fat-fingered the
                # date pickers; the swap is what they wanted anyway.
                cf, ct = ct, cf
            custom_start = cf.toString(Qt.ISODate)
            custom_end = ct.toString(Qt.ISODate)

        categories = self._categories_panel.checked_ids()
        if self._categories_panel.is_all_checked():
            categories = []
        payees = self._payees_panel.checked_ids()
        if self._payees_panel.is_all_checked():
            payees = []
        accounts = self._accounts_panel.checked_ids()
        if self._accounts_panel.is_all_checked():
            accounts = []

        self._result = SpendingOverTimeFilters(
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            granularity=self._granularity_combo.currentData() or "auto",
            rollup_level=self._rollup_combo.currentData() or _ROLLUP_TOP,
            category_ids=tuple(categories),
            include_uncategorised=self._include_uncat_check.isChecked(),
            payee_ids=tuple(payees),
            account_ids=tuple(accounts),
            include_transfers=self._current.include_transfers,
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
        f: SpendingOverTimeFilters,
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
        try:
            return period_bounds(f.period_key, today)
        except ValueError:
            return (today.replace(day=1), today)
