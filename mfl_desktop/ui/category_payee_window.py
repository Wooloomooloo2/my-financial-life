"""Category & Payee — two-level spending drill (ADR-068 / Arc E, E3).

Cross-cuts spending by category and payee. Level 1 ranks the **primary
dimension** (Category or Payee, switchable via a top-bar toggle); clicking a
row drills to a **level-2 breakdown by the other dimension** for that item;
clicking a level-2 row opens the underlying transactions (the shared
``TransactionsListWindow``, filtered by both the category descendants and
the payee). A ← Back button pops level 2 back to level 1.

Spending is **strict outflow** on expense-kind categories (same definition
as Spending Over Time / Payee). The category dimension is the **budget-line
level** (``category_group_map`` — Groceries, Transport…, not the raw
Income/Expense roots); payees roll up to their canonical (ADR-028/029).
Multi-currency totals convert to a chosen display currency (ADR-055:
no-rate slices excluded + noted). Reuses the Payee report's ranked-bar
chart (:class:`PayeeChart`) and ranking (:func:`build_report`); the row id
field carries the current dimension's item id (a category-group id or a
canonical-payee id). No pies (ADR-018).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.account_summary import period_bounds
from mfl_desktop import periods
from mfl_desktop.db.repository import Repository, ReportRow
from mfl_desktop.reports import category_group_map
from mfl_desktop.reports.filters import CategoryPayeeFilters, TYPE_CATEGORY_PAYEE
from mfl_desktop.reports.payee_report import NO_PAYEE_LABEL, build_report
from mfl_desktop.ui.category_payee_filter_dialog import CategoryPayeeFilterDialog
from mfl_desktop.ui.chart_helpers import fmt_currency
from mfl_desktop.ui.payee_chart import PayeeChart
from mfl_desktop.ui.save_report_as_dialog import SaveReportAsDialog
from mfl_desktop.ui.transactions_list_window import (
    TransactionsListWindow, TxnListFilter,
)
from mfl_desktop.ui import tokens
from mfl_desktop.ui.report_save import resolve_save_as

# Period labels reuse account_summary.PERIOD_LABELS (ADR-082, single source).
_CCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥"}


def _symbol_for(currency: str) -> str:
    return _CCY_SYMBOLS.get((currency or "").upper(), "")


def _other(dimension: str) -> str:
    return "payee" if dimension == "category" else "category"


def _dim_label(dimension: str) -> str:
    return "Category" if dimension == "category" else "Payee"


class _NumericItem(QTableWidgetItem):
    """Table cell that sorts by a stored numeric value, not by its text."""

    def __init__(self, text: str, value: float) -> None:
        super().__init__(text)
        self._value = value
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other) -> bool:  # noqa: D401 — Qt override
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


class CategoryPayeeWindow(QMainWindow):
    """Category & Payee window — bare or saved-loaded.

    Construct via :py:meth:`open_bare` (Reports menu) or
    :py:meth:`load_from_id` (a saved-report sidebar click)."""

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

        self.resize(1180, 760)

        # ── reference data ──
        self._all_accounts = repo.list_accounts()
        self._all_categories = repo.list_category_tree()
        self._categories_by_id = {c.id: c for c in self._all_categories}
        self._group_map = category_group_map(self._all_categories)
        self._payee_names = dict(repo.list_canonical_payees())

        self._current_filters: CategoryPayeeFilters = (
            CategoryPayeeFilters.from_json(report.filters_json)
            if report is not None
            else CategoryPayeeFilters.default()
        )
        # The active primary dimension mirrors the filter's group_by; the
        # drill is view-only state (item_id, item_name) — None at level 1.
        self._dimension: str = self._current_filters.group_by
        self._drill: Optional[tuple[Optional[int], str]] = None

        # Cached matrix for the current filters/currency (re-pivoted on
        # toggle/drill without re-querying).
        self._cells: list[dict] = []
        self._unconverted: dict[str, int] = {}

        # ── top bar ──
        self._back_button = QPushButton("← Back")
        self._back_button.clicked.connect(self._on_back)
        self._back_button.setVisible(False)

        self._name_label = QLabel()
        tokens.themed(self._name_label, "color: {heading}; font-weight: bold; padding: 4px 8px;")

        self._group_combo = QComboBox()
        self._group_combo.addItem("Category", "category")
        self._group_combo.addItem("Payee", "payee")
        self._set_combo(self._group_combo, self._dimension)
        self._group_combo.currentIndexChanged.connect(self._on_group_by_changed)

        self._ccy_combo = QComboBox()
        self._ccy_combo.currentIndexChanged.connect(self._on_ccy_changed)

        self._filter_button = QPushButton("Filter…")
        self._filter_button.clicked.connect(self._on_open_filter)
        self._save_button = QPushButton("Save")
        self._save_button.clicked.connect(self._on_save)
        self._save_as_button = QPushButton("Save As…")
        self._save_as_button.clicked.connect(self._on_save_as)

        top_bar = QWidget()
        top_bar_layout = QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(10, 8, 10, 8)
        top_bar_layout.setSpacing(8)
        top_bar_layout.addWidget(self._back_button)
        top_bar_layout.addWidget(self._name_label, stretch=1)
        top_bar_layout.addWidget(QLabel("Group by:"))
        top_bar_layout.addWidget(self._group_combo)
        top_bar_layout.addWidget(QLabel("Display in:"))
        top_bar_layout.addWidget(self._ccy_combo)
        top_bar_layout.addWidget(self._filter_button)
        top_bar_layout.addWidget(self._save_button)
        top_bar_layout.addWidget(self._save_as_button)

        top_rule = QFrame()
        top_rule.setFrameShape(QFrame.HLine)
        top_rule.setFrameShadow(QFrame.Sunken)
        tokens.themed(top_rule, "color: {border};")

        self._breadcrumb = QLabel()
        tokens.themed(self._breadcrumb, "color: {muted_strong}; padding: 6px 12px; background: {canvas};")

        # ── chart over table (left) + summary (right) ──
        self._chart = PayeeChart()
        self._chart.payee_clicked.connect(self._on_item_clicked)
        self._table = self._build_table()
        self._table.cellDoubleClicked.connect(self._on_table_double_clicked)

        self._left_splitter = QSplitter(Qt.Vertical)
        self._left_splitter.addWidget(self._chart)
        self._left_splitter.addWidget(self._table)
        self._left_splitter.setStretchFactor(0, 3)
        self._left_splitter.setStretchFactor(1, 2)

        self._summary_panel = self._build_summary_panel()

        self._body_splitter = QSplitter(Qt.Horizontal)
        self._body_splitter.addWidget(self._left_splitter)
        self._body_splitter.addWidget(self._summary_panel)
        self._body_splitter.setStretchFactor(0, 1)
        self._body_splitter.setStretchFactor(1, 0)

        _f = self._current_filters
        self._left_splitter.setSizes(list(_f.chart_split) if _f.chart_split else [450, 290])
        self._body_splitter.setSizes(list(_f.body_split) if _f.body_split else [900, 280])
        self._left_splitter.splitterMoved.connect(lambda *_: self._mark_dirty())
        self._body_splitter.splitterMoved.connect(lambda *_: self._mark_dirty())
        left_splitter = self._left_splitter
        body_splitter = self._body_splitter

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(top_bar)
        central_layout.addWidget(top_rule)
        central_layout.addWidget(self._breadcrumb)
        central_layout.addWidget(body_splitter, stretch=1)
        self.setCentralWidget(central)

        self._populate_ccy_combo()
        self._update_name_label()
        self._update_save_buttons()
        self._refresh()

    # ── constructors ──

    @classmethod
    def open_bare(cls, repo: Repository, parent=None) -> "CategoryPayeeWindow":
        return cls(repo, report=None, parent=parent)

    @classmethod
    def load_from_id(
        cls, repo: Repository, report_id: int, parent=None,
    ) -> Optional["CategoryPayeeWindow"]:
        report = repo.get_report(report_id)
        if report is None or report.type != TYPE_CATEGORY_PAYEE:
            return None
        return cls(repo, report=report, parent=parent)

    # ── display currency ──

    def _populate_ccy_combo(self) -> None:
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

    # ── detail table ──

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Category", "Spend", "%", "Txns"])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSortingEnabled(True)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        return table

    def _populate_table(self, rows, symbol: str, header0: str) -> None:
        self._table.setSortingEnabled(False)
        self._table.setHorizontalHeaderLabels([header0, "Spend", "%", "Txns"])
        self._table.setRowCount(0)
        prefix = symbol if symbol else f"{self._display_ccy} "
        drillable = self._drill is None
        hint = ("Double-click to drill in" if drillable
                else "Double-click to see transactions")
        for row in rows:
            r = self._table.rowCount()
            self._table.insertRow(r)
            name_item = QTableWidgetItem(row.name)
            name_item.setToolTip(hint)
            name_item.setData(Qt.UserRole, row.payee_id)  # item id (cat/payee)
            self._table.setItem(r, 0, name_item)
            self._table.setItem(
                r, 1,
                _NumericItem(
                    fmt_currency(float(row.amount), 2, symbol=prefix),
                    float(row.amount),
                ),
            )
            self._table.setItem(r, 2, _NumericItem(f"{row.pct * 100:.1f}%", row.pct))
            self._table.setItem(
                r, 3, _NumericItem(str(row.txn_count), float(row.txn_count)),
            )
        self._table.setSortingEnabled(True)

    # ── summary panel ──

    def _build_summary_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.NoFrame)
        tokens.themed(panel, "QFrame { background: {canvas}; border-left: 1px solid {border}; }QLabel { background: transparent; }")
        panel.setMinimumWidth(250)

        self._period_value = QLabel()
        self._period_value.setWordWrap(True)
        tokens.themed(self._period_value, "color: {text};")
        self._filters_value = QLabel()
        self._filters_value.setWordWrap(True)
        tokens.themed(self._filters_value, "color: {muted_strong};")
        self._total_value = QLabel()
        tokens.themed(self._total_value, "color: {text}; font-size: 22px; font-weight: bold;")
        self._rows_value = QLabel()
        self._rows_value.setWordWrap(True)
        tokens.themed(self._rows_value, "color: {muted_strong};")
        self._note_value = QLabel()
        self._note_value.setWordWrap(True)
        tokens.themed(self._note_value, "color: {warning}; font-style: italic;")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        layout.addWidget(self._mini_section_title("Period"))
        layout.addWidget(self._period_value)
        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Filters"))
        layout.addWidget(self._filters_value)
        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Summary"))
        layout.addWidget(self._total_value)
        layout.addWidget(self._rows_value)
        layout.addSpacing(6)
        layout.addWidget(self._note_value)
        layout.addStretch(1)
        return panel

    @staticmethod
    def _mini_section_title(text: str) -> QLabel:
        lab = QLabel(text.upper())
        tokens.themed(lab, "color: {subtle}; font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        return lab

    # ── refresh / pivot / render ──

    def _refresh(self) -> None:
        """Re-query the matrix for the current filters + currency, then
        re-pivot. Called on first show and whenever filters/currency change."""
        filters = self._current_filters
        d_from, d_to = self._resolve_date_bounds(filters)
        account_ids = list(filters.account_ids) or [a.id for a in self._all_accounts]
        if not account_ids:
            self._cells, self._unconverted = [], {}
            self._show_empty("Select at least one account.")
            return
        result = self._repo.category_payee_matrix(
            date_from=d_from.isoformat(),
            date_to=d_to.isoformat(),
            account_ids=account_ids,
            display_currency=self._display_ccy,
            include_transfers=filters.include_transfers,
        )
        self._cells = result["cells"]
        self._unconverted = result.get("unconverted", {})
        self._rebuild_view()

    def _category_label(self, group_id: int) -> str:
        node = self._categories_by_id.get(group_id)
        return node.name if node is not None else f"id={group_id}"

    def _payee_label(self, payee_id: Optional[int]) -> str:
        if payee_id is None:
            return NO_PAYEE_LABEL
        return self._payee_names.get(payee_id, f"id={payee_id}")

    def _aggregate(self, group_dim: str, cells: list[dict]) -> list[dict]:
        """Roll the given cells up by ``group_dim`` ('category' → budget-line
        group, 'payee' → canonical payee) into build_report raw rows. The
        ``payee_id`` key carries the item id whatever the dimension is."""
        acc: dict = {}
        for cell in cells:
            if group_dim == "category":
                item_id = self._group_map.get(cell["category_id"], cell["category_id"])
                name = self._category_label(item_id)
            else:
                item_id = cell["payee_id"]
                name = self._payee_label(item_id)
            entry = acc.get(item_id)
            if entry is None:
                entry = {"payee_id": item_id, "name": name,
                         "spending_pence": 0, "txn_count": 0}
                acc[item_id] = entry
            entry["spending_pence"] += cell["spending_pence"]
            entry["txn_count"] += cell["txn_count"]
        return list(acc.values())

    def _rebuild_view(self) -> None:
        """Pivot the cached matrix for the current dimension + drill and
        render. No re-query — toggling/drilling only re-pivots."""
        if not self._cells:
            self._show_empty("No spending in the selected range / filters.")
            return

        if self._drill is None:
            # Level 1 — rank the primary dimension across all cells.
            row_dim = self._dimension
            raw = self._aggregate(row_dim, self._cells)
        else:
            # Level 2 — the other dimension, within the drilled item.
            row_dim = _other(self._dimension)
            drill_id = self._drill[0]
            if self._dimension == "category":
                subset = [
                    c for c in self._cells
                    if self._group_map.get(c["category_id"], c["category_id"]) == drill_id
                ]
            else:
                subset = [c for c in self._cells if c["payee_id"] == drill_id]
            raw = self._aggregate(row_dim, subset)

        report = build_report(raw, self._current_filters.top_n)
        symbol = _symbol_for(self._display_ccy) or ""
        self._chart.render(rows=report.rows, symbol=symbol or "£")
        self._populate_table(report.rows, symbol, _dim_label(row_dim))
        self._update_breadcrumb(row_dim)
        self._back_button.setVisible(self._drill is not None)
        self._update_summary_panel(report.summary, row_dim)

    def _show_empty(self, message: str) -> None:
        self._chart.show_empty(message)
        self._populate_table([], _symbol_for(self._display_ccy),
                             _dim_label(self._dimension))
        self._update_breadcrumb(self._dimension)
        self._back_button.setVisible(self._drill is not None)
        self._update_summary_panel(None, self._dimension, note=message)

    def _update_breadcrumb(self, row_dim: str) -> None:
        if self._drill is None:
            self._breadcrumb.setText(
                f"By {_dim_label(self._dimension).lower()} — "
                f"click a row to break it down by {_other(self._dimension)}"
            )
        else:
            self._breadcrumb.setText(
                f"{_dim_label(self._dimension)} ▸ {self._drill[1]}  ·  "
                f"by {_dim_label(row_dim).lower()} — click a row for transactions"
            )

    def _fmt(self, value) -> str:
        symbol = _symbol_for(self._display_ccy) or ""
        prefix = symbol if symbol else f"{self._display_ccy} "
        return fmt_currency(float(value), 2, symbol=prefix)

    def _update_summary_panel(self, summary, row_dim: str, note: str = "") -> None:
        filters = self._current_filters
        d_from, d_to = self._resolve_date_bounds(filters)
        period_label = periods.period_label(filters.period_key)
        self._period_value.setText(
            f"{period_label}\n{d_from.isoformat()} → {d_to.isoformat()}"
        )

        top_n_bit = ("Top: all" if filters.top_n <= 0
                     else f"Top: {filters.top_n} per level")
        filter_bits = [
            self._filter_line("Accounts", filters.account_ids,
                              len(self._all_accounts)),
            f"Group by: {_dim_label(self._dimension)}",
            top_n_bit,
            "Transfers: " + ("included" if filters.include_transfers else "excluded"),
        ]
        self._filters_value.setText("\n".join(filter_bits))

        if summary is None:
            self._total_value.setText("—")
            self._rows_value.setText(note)
        else:
            self._total_value.setText(f"Total: {self._fmt(summary.total)}")
            word = _dim_label(row_dim).lower() + ("" if summary.payee_count == 1 else "s")
            if summary.hidden_count > 0:
                self._rows_value.setText(
                    f"Showing top {summary.shown_count} of "
                    f"{summary.payee_count} {word} ({summary.hidden_count} hidden)"
                )
            else:
                self._rows_value.setText(f"{summary.payee_count} {word}")

        if self._unconverted:
            bits = ", ".join(
                f"{_symbol_for(ccy) or (ccy + ' ')}{pence / 100:,.0f}"
                for ccy, pence in sorted(self._unconverted.items())
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

    def _resolve_date_bounds(self, filters: CategoryPayeeFilters) -> tuple[date, date]:
        today = date.today()
        if filters.period_key == "custom":
            try:
                if filters.custom_start and filters.custom_end:
                    return (date.fromisoformat(filters.custom_start),
                            date.fromisoformat(filters.custom_end))
            except ValueError:
                pass
            return period_bounds("1y", today)
        try:
            return period_bounds(filters.period_key, today)
        except ValueError:
            return period_bounds("1y", today)

    # ── drill / toggle / back ──

    def _on_group_by_changed(self, *_a) -> None:
        new_dim = self._group_combo.currentData() or "category"
        if new_dim == self._dimension:
            return
        self._dimension = new_dim
        self._drill = None  # changing the primary axis resets the drill
        self._current_filters = replace(self._current_filters, group_by=new_dim)
        self._mark_dirty()
        self._rebuild_view()

    def _on_item_clicked(self, item_id, name: str) -> None:
        if self._drill is None:
            # Level 1 → drill into the other dimension for this item.
            self._drill = (item_id, name)
            self._rebuild_view()
        else:
            # Level 2 → open the transactions behind this (category, payee).
            self._open_transactions(item_id, name)

    def _on_table_double_clicked(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)
        if item is None:
            return
        self._on_item_clicked(item.data(Qt.UserRole), item.text())

    def _on_back(self) -> None:
        self._drill = None
        self._rebuild_view()

    def _open_transactions(self, leaf_id, leaf_name: str) -> None:
        """Open the transactions for the drilled (category-group, payee)
        pair. Whichever dimension is primary, we end up with one category
        group id and one payee id (the payee expands to canonical+aliases;
        the no-payee group filters to NULL)."""
        if self._dimension == "category":
            cat_group_id, cat_name = self._drill[0], self._drill[1]
            payee_id, payee_name = leaf_id, leaf_name
        else:
            payee_id, payee_name = self._drill[0], self._drill[1]
            cat_group_id, cat_name = leaf_id, leaf_name

        d_from, d_to = self._resolve_date_bounds(self._current_filters)
        acc_ids = list(self._current_filters.account_ids)
        if len(acc_ids) == 1:
            account_id = acc_ids[0]
            account_name = next(
                (a.name for a in self._all_accounts if a.id == account_id), "",
            )
        else:
            account_id, account_name = None, ""

        if payee_id is None:
            payee_ids: tuple[int, ...] = ()
            payee_is_null = True
        else:
            payee_ids = tuple(self._repo.expand_canonical_payee_ids([payee_id]))
            payee_is_null = False

        flt = TxnListFilter(
            account_id=account_id, account_name=account_name,
            category_id=cat_group_id, category_label=cat_name,
            payee_id=(payee_ids[0] if payee_ids else None),
            payee_label=payee_name,
            period_key="custom",
            title_label=f"{cat_name} · {payee_name}",
            custom_start=d_from, custom_end=d_to,
            payee_ids=payee_ids, payee_is_null=payee_is_null,
        )
        win = TransactionsListWindow(self._repo, flt, parent=self)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.show()

    # ── filter dialog ──

    def _on_open_filter(self) -> None:
        dialog = CategoryPayeeFilterDialog(
            self._repo, current=self._current_filters,
            accounts=self._all_accounts, parent=self,
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
        self._dimension = new_filters.group_by
        self._set_combo(self._group_combo, self._dimension)
        self._drill = None
        self._mark_dirty()
        self._refresh()

    # ── save / save-as / dirty state ──

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_save_buttons()

    def _filters_to_persist(self):
        """Current filters with the live splitter sizes folded in (ADR-076)."""
        return replace(
            self._current_filters,
            chart_split=tuple(self._left_splitter.sizes()),
            body_split=tuple(self._body_splitter.sizes()),
        )

    def _on_save(self) -> None:
        if self._report_id is None:
            self._on_save_as()
            return
        try:
            row = self._repo.update_report(
                self._report_id, filters_json=self._filters_to_persist().to_json(),
            )
        except Exception as e:
            QMessageBox.critical(self, "Could not save report",
                                 f"The report was not saved:\n\n{e}")
            return
        self._loaded_name = row.name
        self._loaded_folder_id = row.folder_id
        self._dirty = False
        self._update_name_label()
        self._update_save_buttons()
        self.reports_changed.emit()

    def _on_save_as(self) -> None:
        dialog = SaveReportAsDialog(
            self._repo, initial_name=self._loaded_name,
            initial_folder_id=self._loaded_folder_id,
            title=("Save As…" if self._report_id is not None else "Save report"),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        choice = dialog.values()
        if choice is None:
            return
        try:
            row = resolve_save_as(
                self, self._repo, self._report_id, TYPE_CATEGORY_PAYEE,
                choice.name, choice.folder_id, self._filters_to_persist().to_json(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not save report", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Could not save report",
                                 f"The report was not saved:\n\n{e}")
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
            self._name_label.setText("Untitled Category & Payee")
            tokens.themed(self._name_label, "color: {muted}; font-style: italic; font-weight: bold; padding: 4px 8px;")
            self.setWindowTitle("Category & Payee — Untitled")
            return
        prefix = ""
        if self._loaded_folder_id is not None:
            for f in self._repo.list_report_folders():
                if f.id == self._loaded_folder_id:
                    prefix = f"{f.name} / "
                    break
        dirty_mark = "*" if self._dirty else ""
        self._name_label.setText(f"{prefix}{self._loaded_name}{dirty_mark}")
        tokens.themed(self._name_label, "color: {heading}; font-weight: bold; padding: 4px 8px;")
        self.setWindowTitle(f"Category & Payee — {prefix}{self._loaded_name}{dirty_mark}")

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

    # ── helpers ──

    @staticmethod
    def _set_combo(combo: QComboBox, value: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.blockSignals(True)
                combo.setCurrentIndex(i)
                combo.blockSignals(False)
                return

    # ── close prompt ──

    def closeEvent(self, event) -> None:
        if self._report_id is not None and self._dirty:
            reply = QMessageBox.question(
                self, "Unsaved changes",
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
