"""Income & Expense — cash-flow report (ADR-064 / Arc E, E1).

Non-modal QMainWindow with a top bar (report name / display-currency
selector / filter / save verbs), the combo chart on the left, and a
right-side summary panel showing the period, the account filter, and the
headline figures (total income / expense, net saved, savings rate,
per-bucket averages).

Income vs expense is decided by **category kind** (income = inflows on
income-kind categories, expense = outflows on expense-kind categories,
transfers excluded) — the same convention as the Sankey report. The
aggregation + FX conversion live in :meth:`Repository.income_expense_series`;
the pure :mod:`mfl_desktop.reports.income_expense` module turns the result
into a continuous bucket series + summary. Multi-currency totals convert
to a chosen display currency (ADR-055 policy: no-rate accounts excluded +
noted, never par-added). Renderer is the hand-rolled
:class:`IncomeExpenseChart` paintEvent widget (ADR-026 — no pies).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.account_summary import period_bounds
from mfl_desktop import periods
from mfl_desktop.db.repository import Repository, ReportRow
from mfl_desktop.reports.filters import (
    IncomeExpenseFilters, TYPE_INCOME_EXPENSE,
)
from mfl_desktop.reports.income_expense import (
    bucket_bounds, build_buckets, compose_top_level, compute_summary,
    enumerate_buckets,
)
from mfl_desktop.ui.chart_helpers import colour_for, fmt_currency
from mfl_desktop.ui.donut_chart import DonutChart, DonutSegment
from mfl_desktop.ui.income_expense_chart import IncomeExpenseChart
from mfl_desktop.ui.income_expense_filter_dialog import (
    IncomeExpenseFilterDialog,
)
from mfl_desktop.ui.page_header import PageHeader
from mfl_desktop.ui.save_report_as_dialog import SaveReportAsDialog
from mfl_desktop.ui.transactions_list_window import (
    TransactionsListWindow, TxnListFilter,
)
from mfl_desktop.ui import tokens
from mfl_desktop.ui.report_save import resolve_save_as
from dataclasses import replace

# Dataclass granularity keys → SQL bucket keys (the SQL/pure side speak
# "week" / "month" / ...; the dataclass speaks "weekly" / "monthly").
_GRANULARITY_TO_SQL: dict[str, str] = {
    "weekly":    "week",
    "monthly":   "month",
    "quarterly": "quarter",
    "annually":  "year",
}
_GRANULARITY_WORD: dict[str, str] = {
    "week": "week", "month": "month", "quarter": "quarter", "year": "year",
}
# Period labels reuse account_summary.PERIOD_LABELS (ADR-082, single source).
_CCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥"}


def _symbol_for(currency: str) -> str:
    return _CCY_SYMBOLS.get((currency or "").upper(), "")


def _auto_granularity_for(span_days: int) -> str:
    """Resolve granularity='auto' against a date-span — mirrors the
    Spending report (no daily bucket; cash-flow bars get unreadable at
    daily granularity)."""
    if span_days <= 90:
        return "week"
    if span_days <= 730:
        return "month"
    if span_days <= 2200:
        return "quarter"
    return "year"


class IncomeExpenseWindow(QMainWindow):
    """Income & Expense window — bare or saved-loaded.

    Construct via :py:meth:`open_bare` for an unattached window (the
    Reports menu entry-point) or :py:meth:`load_from_id` for a saved
    report instance (a sidebar click)."""

    reports_changed = Signal()

    def __init__(
        self,
        repo: Repository,
        *,
        report: Optional[ReportRow] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._report_id: Optional[int] = report.id if report is not None else None
        self._loaded_name: Optional[str] = report.name if report is not None else None
        self._loaded_folder_id: Optional[int] = (
            report.folder_id if report is not None else None
        )
        self._dirty: bool = False

        self.resize(1180, 720)

        # ── reference data ──
        # Reports include closed accounts by default (ADR-115) — their history
        # matters for long-term trends; the filter dialog can uncheck them.
        self._all_accounts = repo.list_accounts(include_closed=True)
        self._all_categories = repo.list_category_tree()
        # ADR-143: archived-inclusive tree for the composition donut's name /
        # parent rollup, so a slice for a since-archived category resolves to
        # its name instead of a bare "id=N". The filter picker stays on the
        # live-only ``_all_categories``.
        self._display_categories = repo.list_category_tree(include_archived=True)

        self._current_filters: IncomeExpenseFilters = (
            IncomeExpenseFilters.from_json(report.filters_json)
            if report is not None
            else IncomeExpenseFilters.default()
        )

        # ── page header (ADR-119) ──
        self._ccy_combo = QComboBox()
        self._ccy_combo.currentIndexChanged.connect(self._on_ccy_changed)

        self._filter_button = QPushButton("Filter…")
        self._filter_button.setProperty("mflVariant", "primary")
        self._filter_button.clicked.connect(self._on_open_filter)
        self._save_button = QPushButton("Save")
        self._save_button.setProperty("mflVariant", "ghost")
        self._save_button.clicked.connect(self._on_save)
        self._save_as_button = QPushButton("Save As…")
        self._save_as_button.setProperty("mflVariant", "ghost")
        self._save_as_button.clicked.connect(self._on_save_as)

        self._page_header = PageHeader(show_rule=True)
        self._page_header.add_action(QLabel("Display in:"))
        self._page_header.add_action(self._ccy_combo)
        self._page_header.add_action(self._filter_button)
        self._page_header.add_action(self._save_button)
        self._page_header.add_action(self._save_as_button)

        # ── chart + summary panel ──
        self._chart = IncomeExpenseChart()
        self._chart.segment_clicked.connect(self._on_segment_clicked)
        # Granularity of the last render — the drill resolves a clicked
        # bucket key to its date span against this (ADR-083).
        self._last_granularity: Optional[str] = None
        # Composition donut (ADR-064 amend): which side it shows, and the
        # last-computed slices for each so the toggle re-renders without a
        # re-query. Income/Expense breakdown is a point-in-time composition
        # of a positive whole — the donut exception carved out by ADR-067.
        self._comp_mode: str = "income"
        self._comp_slices: dict[str, list] = {"income": [], "expense": []}
        self._summary_panel = self._build_summary_panel()

        self._body_splitter = QSplitter(Qt.Horizontal)
        self._body_splitter.addWidget(self._chart)
        self._body_splitter.addWidget(self._summary_panel)
        self._body_splitter.setStretchFactor(0, 1)
        self._body_splitter.setStretchFactor(1, 0)
        _bs = self._current_filters.body_split
        self._body_splitter.setSizes(list(_bs) if _bs else [900, 280])
        self._body_splitter.splitterMoved.connect(lambda *_: self._mark_dirty())
        body_splitter = self._body_splitter

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._page_header)
        central_layout.addWidget(body_splitter, stretch=1)
        self.setCentralWidget(central)

        # Resolve the display currency last (this also sets self._display_ccy)
        # then do the first render.
        self._populate_ccy_combo()
        self._update_name_label()
        self._update_save_buttons()
        self._refresh()

    # ── constructors ──

    @classmethod
    def open_bare(cls, repo: Repository, parent=None) -> "IncomeExpenseWindow":
        return cls(repo, report=None, parent=parent)

    @classmethod
    def load_from_id(
        cls, repo: Repository, report_id: int, parent=None,
    ) -> Optional["IncomeExpenseWindow"]:
        report = repo.get_report(report_id)
        if report is None or report.type != TYPE_INCOME_EXPENSE:
            return None
        return cls(repo, report=report, parent=parent)

    # ── display currency ──

    def _populate_ccy_combo(self) -> None:
        """Fill the display-currency selector from the currencies in use,
        defaulting to the base currency (then GBP, then the first in use).
        Like Net Worth (ADR-055) / Sankey (ADR-056) this is a view
        preference — not persisted in the saved filters."""
        currencies = self._repo.list_distinct_currencies()
        base = self._repo.get_setting("base_currency")
        options = sorted(set(currencies) | ({base} if base else set()))
        if not options:
            options = ["GBP"]
        if base and base in options:
            default = base
        elif "GBP" in options:
            default = "GBP"
        else:
            default = options[0]
        self._display_ccy = default
        self._ccy_combo.blockSignals(True)
        self._ccy_combo.clear()
        for ccy in options:
            self._ccy_combo.addItem(ccy, ccy)
        i = self._ccy_combo.findData(default)
        self._ccy_combo.setCurrentIndex(i if i >= 0 else 0)
        self._ccy_combo.blockSignals(False)

    def _on_ccy_changed(self, *_a) -> None:
        self._display_ccy = self._ccy_combo.currentData() or "GBP"
        self._refresh()

    # ── right-side summary panel ──

    def _build_summary_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.NoFrame)
        tokens.themed(panel, "QFrame { background: {canvas}; border-left: 1px solid {border}; }QLabel { background: transparent; }")
        panel.setMinimumWidth(250)

        self._period_value = QLabel()
        self._period_value.setWordWrap(True)
        tokens.themed(self._period_value, "color: {text};")
        self._granularity_value = QLabel()
        tokens.themed(self._granularity_value, "color: {text};")
        self._filters_value = QLabel()
        self._filters_value.setWordWrap(True)
        tokens.themed(self._filters_value, "color: {muted_strong};")

        self._income_value = QLabel()
        tokens.themed(self._income_value, "color: {positive}; font-weight: bold;")
        self._expense_value = QLabel()
        tokens.themed(self._expense_value, "color: {negative_strong}; font-weight: bold;")
        self._net_value = QLabel()
        tokens.themed(self._net_value, "color: {text}; font-size: 22px; font-weight: bold;")
        self._savings_rate_value = QLabel()
        tokens.themed(self._savings_rate_value, "color: {muted_strong};")
        self._avg_value = QLabel()
        self._avg_value.setWordWrap(True)
        tokens.themed(self._avg_value, "color: {muted_strong};")
        self._note_value = QLabel()
        self._note_value.setWordWrap(True)
        tokens.themed(self._note_value, "color: {warning}; font-style: italic;")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        layout.addWidget(self._mini_section_title("Period"))
        layout.addWidget(self._period_value)
        layout.addWidget(self._granularity_value)

        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Filters"))
        layout.addWidget(self._filters_value)

        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Summary"))
        layout.addWidget(self._income_value)
        layout.addWidget(self._expense_value)
        layout.addWidget(self._net_value)
        layout.addWidget(self._savings_rate_value)
        layout.addWidget(self._avg_value)
        layout.addSpacing(6)
        layout.addWidget(self._note_value)
        layout.addStretch(1)

        # ── breakdown donut (pinned to the bottom of the panel) ──
        layout.addWidget(self._mini_section_title("Breakdown"))
        layout.addLayout(self._build_comp_toggle())
        self._comp_donut = DonutChart()
        self._comp_donut.setMinimumHeight(180)
        layout.addWidget(self._comp_donut)

        # Legend: one row per slice (swatch · name · amount), rebuilt per
        # render. Replaces the donut's centre total (ADR-113).
        self._comp_legend = QVBoxLayout()
        self._comp_legend.setContentsMargins(0, 4, 0, 0)
        self._comp_legend.setSpacing(3)
        layout.addLayout(self._comp_legend)
        return panel

    def _build_comp_toggle(self) -> QHBoxLayout:
        """A two-button Income / Expense segmented toggle over the donut."""
        self._comp_income_btn = QPushButton("Income")
        self._comp_expense_btn = QPushButton("Expense")
        for b in (self._comp_income_btn, self._comp_expense_btn):
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            tokens.themed(
                b,
                "QPushButton { padding: 4px 12px; border: 1px solid {border_strong}; "
                "border-radius: 13px; background-color: {surface}; color: {heading}; "
                "font-size: 12px; }"
                "QPushButton:checked { background-color: {accent}; color: {surface}; "
                "border-color: {accent}; font-weight: bold; }"
                "QPushButton:hover:!checked { background-color: {surface_alt}; }",
            )
        self._comp_income_btn.setChecked(True)
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self._comp_income_btn)
        group.addButton(self._comp_expense_btn)
        self._comp_income_btn.clicked.connect(lambda: self._set_comp_mode("income"))
        self._comp_expense_btn.clicked.connect(lambda: self._set_comp_mode("expense"))

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self._comp_income_btn)
        row.addWidget(self._comp_expense_btn)
        return row

    def _set_comp_mode(self, mode: str) -> None:
        if mode == self._comp_mode:
            return
        self._comp_mode = mode
        # Keep the toggle in sync even when called programmatically.
        self._comp_income_btn.setChecked(mode == "income")
        self._comp_expense_btn.setChecked(mode == "expense")
        self._render_comp_donut()

    @staticmethod
    def _mini_section_title(text: str) -> QLabel:
        lab = QLabel(text.upper())
        tokens.themed(lab, "color: {subtle}; font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        return lab

    # ── refresh / render ──

    def _refresh(self) -> None:
        filters = self._current_filters
        d_from, d_to = self._resolve_date_bounds(filters)

        sql_granularity = (
            _auto_granularity_for((d_to - d_from).days)
            if filters.granularity == "auto"
            else _GRANULARITY_TO_SQL.get(filters.granularity, "month")
        )

        account_ids = list(filters.account_ids) or [a.id for a in self._all_accounts]
        if not account_ids:
            self._show_empty("Select at least one account.")
            return

        # Expand each picked category to its subtree so selecting a parent
        # (e.g. "Expense") pulls in its children (ADR-088 amend). Empty == all.
        category_ids = self._expanded_category_ids(filters.category_ids)
        if filters.category_ids and not category_ids:
            self._show_empty("No transactions match the selected categories.")
            return

        # ADR-140: expand any picked transfer categories to their subtree too,
        # so selecting a parent transfer group pulls in its children.
        transfer_category_ids = self._expanded_category_ids(
            filters.transfer_category_ids
        )
        result = self._repo.income_expense_series(
            date_from=d_from.isoformat(),
            date_to=d_to.isoformat(),
            granularity=sql_granularity,
            account_ids=account_ids,
            category_ids=category_ids,
            display_currency=self._display_ccy,
            include_transfers=filters.include_transfers,
            transfer_category_ids=transfer_category_ids,
        )

        self._last_granularity = sql_granularity
        bucket_order = enumerate_buckets(d_from, d_to, sql_granularity)
        buckets = build_buckets(
            bucket_order, result["income"], result["expense"],
        )
        self._render(buckets, sql_granularity, d_from, d_to, filters,
                     result.get("unconverted", {}))
        self._refresh_composition(
            d_from, d_to, account_ids, category_ids,
            include_transfers=filters.include_transfers,
            transfer_category_ids=transfer_category_ids,
        )

    def _render(
        self,
        buckets,
        granularity: str,
        d_from: date,
        d_to: date,
        filters: IncomeExpenseFilters,
        unconverted: dict,
    ) -> None:
        if not buckets:
            self._show_empty("No income or expense in the selected range.")
            return

        summary = compute_summary(buckets)
        symbol = _symbol_for(self._display_ccy) or ""

        self._chart.render(
            buckets=buckets,
            avg_income=float(summary.avg_income),
            avg_expense=float(summary.avg_expense),
            symbol=symbol or "£",
        )
        self._update_summary_panel(
            filters=filters, d_from=d_from, d_to=d_to,
            granularity=granularity, summary=summary, unconverted=unconverted,
        )

    # ── breakdown donut (ADR-064 amend / ADR-067 donut exception) ──

    def _refresh_composition(
        self,
        d_from: date,
        d_to: date,
        account_ids: list[int],
        category_ids: Optional[list[int]],
        *,
        include_transfers: bool = False,
        transfer_category_ids: Optional[list[int]] = None,
    ) -> None:
        """Recompute the income / expense top-level breakdowns for the donut.

        Uses :meth:`Repository.sankey_category_totals` — the same category-
        kind cash-flow convention as the headline figures, including the
        directional-transfer folding (ADR-140) so the donut matches the bars —
        and rolls each leaf total up to its top-level category. Stored per side
        so the toggle re-renders without re-querying."""
        totals = self._repo.sankey_category_totals(
            date_from=d_from.isoformat(),
            date_to=d_to.isoformat(),
            account_ids=account_ids,
            category_ids=category_ids,
            display_currency=self._display_ccy,
            include_transfers=include_transfers,
            transfer_category_ids=transfer_category_ids,
        )
        parent_of = {c.id: c.parent_id for c in self._display_categories}
        name_of = {c.id: c.name for c in self._display_categories}
        self._comp_slices = {
            side: compose_top_level(totals.get(side, {}), parent_of, name_of)
            for side in ("income", "expense")
        }
        self._render_comp_donut()

    def _render_comp_donut(self) -> None:
        slices = self._comp_slices.get(self._comp_mode, [])
        symbol = _symbol_for(self._display_ccy) or "£"
        prefix = symbol if symbol else f"{self._display_ccy} "
        self._clear_comp_legend()
        if not slices:
            self._comp_donut.show_empty(f"No {self._comp_mode} in range")
            return
        colours = [colour_for(i) for i in range(len(slices))]
        segments = [
            DonutSegment(label=s.label, value=float(s.value), color=colours[i])
            for i, s in enumerate(slices)
        ]
        # Flat single ring + no centre total (the headline already shows it).
        self._comp_donut.set_data(
            segments=segments, center_label="", center_sub="",
            symbol=prefix, two_ring=False,
        )
        for s, col in zip(slices, colours):
            self._comp_legend.addLayout(
                self._legend_row(s.label, self._fmt(s.value, 0), col)
            )

    def _clear_comp_legend(self) -> None:
        """Tear down the legend rows (nested layouts + their labels). Widgets
        are reparented away immediately so they stop painting at once, then
        scheduled for deletion."""
        while self._comp_legend.count():
            item = self._comp_legend.takeAt(0)
            child = item.layout()
            if child is not None:
                while child.count():
                    sub = child.takeAt(0)
                    w = sub.widget()
                    if w is not None:
                        w.setParent(None)
                        w.deleteLater()
                child.deleteLater()

    def _legend_row(self, name: str, amount: str, colour) -> QHBoxLayout:
        swatch = QLabel()
        swatch.setFixedSize(10, 10)
        swatch.setStyleSheet(
            f"background: {colour.name()}; border-radius: 2px;"
        )
        name_lab = QLabel(name)
        name_lab.setToolTip(name)
        tokens.themed(name_lab, "color: {text}; font-size: 11px; background: transparent;")
        amount_lab = QLabel(amount)
        tokens.themed(amount_lab, "color: {muted_strong}; font-size: 11px; background: transparent;")
        amount_lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(swatch)
        row.addWidget(name_lab, stretch=1)
        row.addWidget(amount_lab)
        return row

    def _show_empty(self, message: str) -> None:
        self._chart.show_empty(message)
        self._comp_slices = {"income": [], "expense": []}
        self._render_comp_donut()
        self._update_summary_panel(
            filters=self._current_filters, d_from=None, d_to=None,
            granularity=None, summary=None, unconverted={}, note=message,
        )

    def _on_segment_clicked(self, kind: str, bucket_key: str) -> None:
        """Drill a clicked income / expense bar to its transactions (ADR-083).
        The bar's bucket key resolves to a date span; the drill scopes to that
        span + the cash-flow kind (income inflows / expense outflows, transfers
        excluded), matching the bar's value."""
        if self._last_granularity is None:
            return
        try:
            d_from, d_to = bucket_bounds(bucket_key, self._last_granularity)
        except ValueError:
            return
        # Account scope: a single selected account drills per-account; 0 (all)
        # or a subset opens the cross-account view (mirrors the Payee report).
        acc_ids = list(self._current_filters.account_ids)
        if len(acc_ids) == 1:
            account_id: Optional[int] = acc_ids[0]
            account_name = next(
                (a.name for a in self._all_accounts if a.id == account_id), "",
            )
        else:
            account_id, account_name = None, ""
        kind_label = "Income" if kind == "income" else "Expense"
        flt = TxnListFilter.for_kind(
            account_id=account_id, account_name=account_name,
            kind=kind, kind_label=kind_label,
            period_key="custom", custom_start=d_from, custom_end=d_to,
        )
        win = TransactionsListWindow(self._repo, flt, parent=self)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.show()

    def _fmt(self, value, decimals: int = 2) -> str:
        symbol = _symbol_for(self._display_ccy) or ""
        prefix = symbol if symbol else f"{self._display_ccy} "
        return fmt_currency(float(value), decimals, symbol=prefix)

    def _update_summary_panel(
        self,
        *,
        filters: IncomeExpenseFilters,
        d_from: Optional[date],
        d_to: Optional[date],
        granularity: Optional[str],
        summary,
        unconverted: dict,
        note: Optional[str] = None,
    ) -> None:
        period_label = periods.period_label(filters.period_key)
        if d_from is not None and d_to is not None:
            self._period_value.setText(
                f"{period_label}\n{d_from.isoformat()} → {d_to.isoformat()}"
            )
        else:
            self._period_value.setText(period_label)

        if granularity is not None:
            self._granularity_value.setText(
                f"Granularity: {filters.granularity}"
                + ("" if filters.granularity != "auto" else f" → {granularity}")
            )
        else:
            self._granularity_value.setText(f"Granularity: {filters.granularity}")

        filter_bits = [
            self._filter_line(
                "Accounts", filters.account_ids, len(self._all_accounts),
            ),
            self._filter_line(
                "Categories", filters.category_ids, self._category_count(),
            ),
            self._transfers_filter_line(filters),
        ]
        self._filters_value.setText("\n".join(filter_bits))

        if summary is None:
            self._income_value.setText("")
            self._expense_value.setText("")
            self._net_value.setText("—")
            self._savings_rate_value.setText(note or "")
            self._avg_value.setText("")
        else:
            gran_word = _GRANULARITY_WORD.get(granularity or "month", "month")
            self._income_value.setText(
                f"Income: {self._fmt(summary.total_income)}"
            )
            self._expense_value.setText(
                f"Expense: {self._fmt(summary.total_expense)}"
            )
            net_word = "saved" if summary.net >= 0 else "overspent"
            self._net_value.setText(
                f"Net {net_word}: {self._fmt(abs(summary.net))}"
            )
            self._net_value.setStyleSheet(
                "font-size: 22px; font-weight: bold; color: "
                + ("#047857;" if summary.net >= 0 else "#b91c1c;")
            )
            if summary.savings_rate is None:
                self._savings_rate_value.setText("Savings rate: —")
            else:
                self._savings_rate_value.setText(
                    f"Savings rate: {summary.savings_rate * 100:.1f}%"
                )
            self._avg_value.setText(
                f"Avg income: {self._fmt(summary.avg_income)} / {gran_word}\n"
                f"Avg expense: {self._fmt(summary.avg_expense)} / {gran_word}"
            )

        # Excluded-currency note (ADR-055 policy — no-rate amounts are
        # dropped from the totals, never par-added).
        if unconverted:
            bits = ", ".join(
                f"{_symbol_for(ccy) or (ccy + ' ')}{pence / 100:,.0f}"
                for ccy, pence in sorted(unconverted.items())
            )
            self._note_value.setText(
                f"Excluded (no rate to {self._display_ccy}): {bits}"
            )
        else:
            self._note_value.setText("")

    @staticmethod
    def _filter_line(label: str, selected: tuple, total: int) -> str:
        if not selected:
            return f"{label}: all"
        return f"{label}: {len(selected)} of {total}"

    @staticmethod
    def _transfers_filter_line(filters) -> str:
        """Summary line for the transfer inclusion (ADR-140)."""
        if not filters.include_transfers:
            return "Transfers: excluded"
        n = len(filters.transfer_category_ids)
        if not n:
            return "Transfers: included (all categories)"
        return f"Transfers: included ({n} categor{'y' if n == 1 else 'ies'})"

    def _expanded_category_ids(
        self, selected: tuple[int, ...],
    ) -> Optional[list[int]]:
        """Expand the picked category ids to their full subtrees (ADR-088
        amend) so a parent selection includes its descendants. Returns
        ``None`` when nothing is selected (the repo's "all categories"
        signal); otherwise the de-duplicated descendant id list."""
        if not selected:
            return None
        ids: set[int] = set()
        for cid in selected:
            ids |= self._repo.category_descendants(cid)
        return sorted(ids)

    def _category_count(self) -> int:
        """How many income/expense categories exist — the denominator for
        the 'Categories: N of M' summary line."""
        return sum(
            1 for c in self._all_categories if c.kind in ("income", "expense")
        )

    def _resolve_date_bounds(
        self, filters: IncomeExpenseFilters,
    ) -> tuple[date, date]:
        today = date.today()
        if filters.period_key == "custom":
            try:
                if filters.custom_start and filters.custom_end:
                    return (
                        date.fromisoformat(filters.custom_start),
                        date.fromisoformat(filters.custom_end),
                    )
            except ValueError:
                pass
            return period_bounds("1y", today)
        try:
            return period_bounds(filters.period_key, today)
        except ValueError:
            return period_bounds("1y", today)

    # ── filter dialog ──

    def _on_open_filter(self) -> None:
        dialog = IncomeExpenseFilterDialog(
            self._repo,
            current=self._current_filters,
            accounts=self._all_accounts,
            categories=self._all_categories,
            parent=self,
        )
        accepted = dialog.exec() == QDialog.Accepted
        # ADR-105: keep this report in front after the modal filter closes.
        self.raise_()
        self.activateWindow()
        if not accepted:
            return
        new_filters = dialog.values()
        if new_filters is None or new_filters == self._current_filters:
            return
        self._current_filters = new_filters
        self._mark_dirty()
        self._refresh()

    # ── save / save-as / dirty state ──

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_save_buttons()

    def _filters_to_persist(self):
        """Current filters with the live splitter size folded in (ADR-076)."""
        return replace(
            self._current_filters,
            body_split=tuple(self._body_splitter.sizes()),
        )

    def _on_save(self) -> None:
        if self._report_id is None:
            self._on_save_as()
            return
        try:
            row = self._repo.update_report(
                self._report_id,
                filters_json=self._filters_to_persist().to_json(),
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Could not save report",
                f"The report was not saved:\n\n{e}",
            )
            return
        self._loaded_name = row.name
        self._loaded_folder_id = row.folder_id
        self._dirty = False
        self._update_name_label()
        self._update_save_buttons()
        self.reports_changed.emit()

    def _on_save_as(self) -> None:
        dialog = SaveReportAsDialog(
            self._repo,
            initial_name=self._loaded_name,
            initial_folder_id=self._loaded_folder_id,
            title=(
                "Save As…" if self._report_id is not None else "Save report"
            ),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        choice = dialog.values()
        if choice is None:
            return
        try:
            row = resolve_save_as(
                self, self._repo, self._report_id, TYPE_INCOME_EXPENSE,
                choice.name, choice.folder_id, self._filters_to_persist().to_json(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not save report", str(e))
            return
        except Exception as e:
            QMessageBox.critical(
                self, "Could not save report",
                f"The report was not saved:\n\n{e}",
            )
            return
        if row is None:
            return
        self._report_id = row.id
        self._loaded_name = row.name
        self._loaded_folder_id = row.folder_id
        self._dirty = False
        self._update_name_label()
        self._update_save_buttons()
        self.reports_changed.emit()

    def _update_name_label(self) -> None:
        if self._loaded_name is None:
            self._page_header.set_heading("Untitled", "Income & Expense")
            self.setWindowTitle("Income & Expense — Untitled")
            return
        prefix = ""
        if self._loaded_folder_id is not None:
            for f in self._repo.list_report_folders():
                if f.id == self._loaded_folder_id:
                    prefix = f"{f.name} / "
                    break
        dirty_mark = "*" if self._dirty else ""
        self._page_header.set_heading(f"{prefix}{self._loaded_name}{dirty_mark}", "Income & Expense")
        self.setWindowTitle(
            f"Income & Expense — {prefix}{self._loaded_name}{dirty_mark}"
        )

    def _update_save_buttons(self) -> None:
        if self._report_id is None:
            self._save_button.setText("Save As…")
            self._save_button.setEnabled(True)
            self._save_as_button.setVisible(False)
        else:
            self._save_button.setText("Save")
            self._save_button.setEnabled(self._dirty)
            self._save_as_button.setVisible(True)
        self._update_name_label()

    # ── close prompt ──

    def closeEvent(self, event) -> None:
        if self._report_id is not None and self._dirty:
            reply = QMessageBox.question(
                self,
                "Unsaved changes",
                f"‘{self._loaded_name}’ has unsaved changes. Save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if reply == QMessageBox.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.Save:
                self._on_save()
        super().closeEvent(event)
