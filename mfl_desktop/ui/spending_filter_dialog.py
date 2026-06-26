"""Filter dialog for the Spending / Income Over Time reports (ADR-039
follow-up; generalised for income in ADR-088).

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

The period/granularity/accounts plumbing + the OK/Cancel scaffold come
from :class:`ReportFilterDialogBase` (ADR-084); only the rollup +
categories/payees specials live here.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

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
from mfl_desktop.ui.report_filter_dialog_base import ReportFilterDialogBase
from dataclasses import replace

# Shared with the report window.
UNCATEGORISED_ID = 1

_ROLLUP_TOP = "top"
_ROLLUP_GROUP = "group"
_ROLLUP_LEAF = "leaf"
_ROLLUP_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Top level", _ROLLUP_TOP),
    ("Group",     _ROLLUP_GROUP),
    ("Leaf",      _ROLLUP_LEAF),
)


class SpendingFilterDialog(ReportFilterDialogBase):
    """Modal filter editor — single trip in / out, accept commits."""

    def __init__(
        self,
        repo: Repository,
        *,
        current: SpendingOverTimeFilters,
        accounts: list[AccountSummary],
        categories: list[CategoryNode],
        canonical_payees: list[tuple[int, str]],
        kind: str = "expense",
        title: str = "Filter — Spending Over Time",
        parent=None,
    ) -> None:
        super().__init__(parent, title=title)
        self.resize(820, 620)

        self._repo = repo
        self._current = current
        # The category kind this report counts ("expense" for Spending Over
        # Time, "income" for Income Over Time — ADR-088). Drives which
        # categories populate the checklist and whether the Uncategorised
        # toggle is meaningful (only expense has an Uncategorised bucket).
        self._kind = kind
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

        # ── left column: period + granularity + rollup + uncat toggle ──
        period_combo = self._make_period_combo(
            SPENDING_PERIOD_KEYS, current.period_key,
        )
        custom_from, custom_to = self._make_custom_dates(
            current.period_key, current.custom_start, current.custom_end,
        )
        granularity_combo = self._make_granularity_combo(current.granularity)

        self._rollup_combo = QComboBox()
        for label, value in _ROLLUP_OPTIONS:
            self._rollup_combo.addItem(label, userData=value)
        self._set_combo_to(self._rollup_combo, current.rollup_level)
        self._rollup_combo.currentIndexChanged.connect(self._on_rollup_changed)

        self._include_uncat_check = QCheckBox("Include Uncategorised")
        self._include_uncat_check.setChecked(current.include_uncategorised)
        # The Uncategorised category (id=1) is kind='expense' (ADR-014), so an
        # income report can never surface it — hide the toggle there (ADR-088).
        self._include_uncat_check.setVisible(self._kind == "expense")

        # Income-only: fold in reinvested-dividend (DRIP) income, valued at
        # quantity × price since these rows carry no cash (ADR-089). Hidden for
        # the spending report, which has no such concept.
        self._include_reinv_check = QCheckBox("Show Reinvested Dividends")
        self._include_reinv_check.setToolTip(
            "Reinvested distributions (DRIP — ReinvDiv) carry their dividend as\n"
            "new shares, not cash. When on, they appear as their own\n"
            "“Reinvested Dividends” series in the chart (valued at quantity ×\n"
            "price), independent of the category filter — so you can see\n"
            "reinvested income distinctly from your cash dividends."
        )
        self._include_reinv_check.setChecked(
            getattr(current, "include_reinvested_dividends", True)
        )
        self._include_reinv_check.setVisible(self._kind == "income")

        period_box = QGroupBox("Period")
        period_form = QFormLayout(period_box)
        period_form.addRow("Preset:", period_combo)
        period_form.addRow("From:", custom_from)
        period_form.addRow("To:", custom_to)

        shape_box = QGroupBox("Shape")
        shape_form = QFormLayout(shape_box)
        shape_form.addRow("Granularity:", granularity_combo)
        shape_form.addRow("Rollup:", self._rollup_combo)
        shape_form.addRow(self._include_uncat_check)
        shape_form.addRow(self._include_reinv_check)

        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(period_box)
        left_layout.addWidget(shape_box)
        left_layout.addStretch(1)

        # ── right column: three checklists side-by-side ──
        accounts_panel = self._make_accounts_panel(
            accounts, current.account_ids,
        )

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
        lists_splitter.addWidget(accounts_panel)
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

        self._finalise(top_splitter)
        self._sync_custom_visibility()

    # ── internals ──

    def _category_rows_for_rollup(self, rollup: str) -> list[tuple[int, str]]:
        """Return ``(id, full_path_label)`` rows for the distinct
        bucket-ids that the rollup map produces over the report's kind
        (expense or income — ADR-088) categories. Uncategorised is excluded
        — its own toggle covers it (and it's expense-only anyway). Sorted by
        full breadcrumb (ADR-031) so siblings cluster."""
        rollup_map = self._rollup_maps[rollup]
        bucket_ids: set[int] = set()
        for c in self._all_categories:
            if c.kind == self._kind:
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

    def _on_accept(self) -> None:
        period_key, custom_start, custom_end = self._period_and_custom("quarter")

        # ``replace`` preserves the concrete filter type — SpendingOverTimeFilters
        # for the spending report, IncomeOverTimeFilters for income (ADR-088) —
        # and carries the saved splitter sizes through untouched.
        changes = dict(
            period_key=period_key,
            custom_start=custom_start,
            custom_end=custom_end,
            granularity=self._granularity_combo.currentData() or "auto",
            rollup_level=self._rollup_combo.currentData() or _ROLLUP_TOP,
            category_ids=tuple(self._checked_or_all(self._categories_panel)),
            include_uncategorised=self._include_uncat_check.isChecked(),
            payee_ids=tuple(self._checked_or_all(self._payees_panel)),
            account_ids=tuple(self._checked_or_all(self._accounts_panel)),
            include_transfers=self._current.include_transfers,
        )
        # Income-only field (ADR-089) — only set it when the current filter
        # actually has it, else ``replace`` on a spending filter would raise.
        if hasattr(self._current, "include_reinvested_dividends"):
            changes["include_reinvested_dividends"] = (
                self._include_reinv_check.isChecked()
            )
        self._result = replace(self._current, **changes)
        self.accept()
