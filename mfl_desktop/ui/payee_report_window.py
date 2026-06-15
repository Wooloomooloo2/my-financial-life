"""Payee — ranked spending-by-payee report (ADR-066 / Arc E, E2).

Non-modal QMainWindow with a top bar (report name / display-currency
selector / filter / save verbs), a ranked-bar chart over a sortable detail
table on the left, and a right-side summary panel (period, filters,
headline figures).

Spending is **strict outflow** on expense-kind categories — the same
definition as Spending Over Time (ADR-018) — aggregated per **canonical
payee** (aliases rolled up, ADR-028/029). The aggregation + FX conversion
live in :meth:`Repository.payee_spending_aggregates`; the pure
:mod:`mfl_desktop.reports.payee_report` module ranks the payees and folds
the long tail into "Other". Multi-currency totals convert to a chosen
display currency (ADR-055 policy: no-rate slices excluded + noted, never
par-added). Renderer is the hand-rolled :class:`PayeeChart` paintEvent
widget (ADR-026 — no pies).
"""
from __future__ import annotations

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
from mfl_desktop.db.repository import Repository, ReportRow
from mfl_desktop.reports.filters import PayeeReportFilters, TYPE_PAYEE
from mfl_desktop.reports.payee_report import build_report
from mfl_desktop.ui.chart_helpers import fmt_currency
from mfl_desktop.ui.payee_chart import PayeeChart
from mfl_desktop.ui.payee_filter_dialog import PayeeFilterDialog
from mfl_desktop.ui.save_report_as_dialog import SaveReportAsDialog
from mfl_desktop.ui.transactions_list_window import (
    TransactionsListWindow, TxnListFilter,
)
from mfl_desktop.ui import tokens

_PERIOD_LABELS: dict[str, str] = {
    "quarter": "Last Quarter",
    "6m":      "Last 6 months",
    "ytd":     "Year to date",
    "1y":      "Last 12 months",
    "3y":      "Last 3 years",
    "custom":  "Custom",
}
_CCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥"}


def _symbol_for(currency: str) -> str:
    return _CCY_SYMBOLS.get((currency or "").upper(), "")


class _NumericItem(QTableWidgetItem):
    """Table cell that sorts by a stored numeric value, not by its text —
    so "£1,200" sorts above "£900" and "5%" above "12%" correctly."""

    def __init__(self, text: str, value: float) -> None:
        super().__init__(text)
        self._value = value
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other) -> bool:  # noqa: D401 — Qt override
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


class PayeeReportWindow(QMainWindow):
    """Payee report window — bare or saved-loaded.

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

        self.resize(1180, 740)

        # ── reference data ──
        self._all_accounts = repo.list_accounts()

        self._current_filters: PayeeReportFilters = (
            PayeeReportFilters.from_json(report.filters_json)
            if report is not None
            else PayeeReportFilters.default()
        )

        # ── top bar ──
        self._name_label = QLabel()
        tokens.themed(self._name_label, "color: {heading}; font-weight: bold; padding: 4px 8px;")

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
        top_bar_layout.addWidget(self._name_label, stretch=1)
        top_bar_layout.addWidget(QLabel("Display in:"))
        top_bar_layout.addWidget(self._ccy_combo)
        top_bar_layout.addWidget(self._filter_button)
        top_bar_layout.addWidget(self._save_button)
        top_bar_layout.addWidget(self._save_as_button)

        top_rule = QFrame()
        top_rule.setFrameShape(QFrame.HLine)
        top_rule.setFrameShadow(QFrame.Sunken)
        tokens.themed(top_rule, "color: {border};")

        # ── chart over table (left) ──
        self._chart = PayeeChart()
        self._chart.payee_clicked.connect(self._open_payee_transactions)
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

        # Restore saved splitter sizes (else sensible defaults) so a layout
        # the owner tuned and saved survives a reopen.
        f = self._current_filters
        self._left_splitter.setSizes(list(f.chart_split) if f.chart_split else [440, 280])
        self._body_splitter.setSizes(list(f.body_split) if f.body_split else [900, 280])
        left_splitter = self._left_splitter
        body_splitter = self._body_splitter

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(top_bar)
        central_layout.addWidget(top_rule)
        central_layout.addWidget(body_splitter, stretch=1)
        self.setCentralWidget(central)

        self._populate_ccy_combo()
        self._update_name_label()
        self._update_save_buttons()
        self._refresh()

    # ── constructors ──

    @classmethod
    def open_bare(cls, repo: Repository, parent=None) -> "PayeeReportWindow":
        return cls(repo, report=None, parent=parent)

    @classmethod
    def load_from_id(
        cls, repo: Repository, report_id: int, parent=None,
    ) -> Optional["PayeeReportWindow"]:
        report = repo.get_report(report_id)
        if report is None or report.type != TYPE_PAYEE:
            return None
        return cls(repo, report=report, parent=parent)

    # ── display currency ──

    def _populate_ccy_combo(self) -> None:
        """Fill the display-currency selector from the currencies in use,
        defaulting to the base currency (then GBP, then the first in use).
        A view preference, not a saved filter (matching Income & Expense)."""
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
        table.setHorizontalHeaderLabels(["Payee", "Spend", "%", "Txns"])
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

    def _populate_table(self, rows, symbol: str) -> None:
        # Disable sorting while inserting, else rows reshuffle mid-populate.
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        prefix = symbol if symbol else f"{self._display_ccy} "
        for row in rows:
            r = self._table.rowCount()
            self._table.insertRow(r)
            name_item = QTableWidgetItem(row.name)
            name_item.setToolTip("Double-click to see transactions")
            # Stash the canonical payee id (None for the no-payee group) so
            # a double-click can open that payee's transactions regardless
            # of the current sort order.
            name_item.setData(Qt.UserRole, row.payee_id)
            self._table.setItem(r, 0, name_item)
            self._table.setItem(
                r, 1,
                _NumericItem(
                    fmt_currency(float(row.amount), 2, symbol=prefix),
                    float(row.amount),
                ),
            )
            self._table.setItem(
                r, 2,
                _NumericItem(f"{row.pct * 100:.1f}%", row.pct),
            )
            self._table.setItem(
                r, 3, _NumericItem(str(row.txn_count), float(row.txn_count)),
            )
        self._table.setSortingEnabled(True)

    # ── drill to transactions ──

    def _on_table_double_clicked(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)
        if item is None:
            return
        payee_id = item.data(Qt.UserRole)  # int, or None for no-payee
        self._open_payee_transactions(payee_id, item.text())

    def _open_payee_transactions(self, payee_id, name: str) -> None:
        """Open a Transactions drill-down scoped to this payee over the
        report's current date range. Aliases roll up to the canonical
        payee in the report, so we expand back to the full id set (canonical
        + aliases) here; the no-payee group (``payee_id is None``) filters to
        transactions with no payee at all."""
        d_from, d_to = self._resolve_date_bounds(self._current_filters)

        # Account scope: a single selected account maps cleanly to the
        # drill-down's per-account view; 0 (all) or a subset opens the
        # cross-account view (the drill-down can't represent an account
        # subset, so a subset slightly over-includes — noted in ADR-066).
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

        flt = TxnListFilter.for_payees(
            account_id=account_id, account_name=account_name,
            payee_ids=payee_ids, payee_label=name, payee_is_null=payee_is_null,
            period_key="custom", custom_start=d_from, custom_end=d_to,
        )
        win = TransactionsListWindow(self._repo, flt, parent=self)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.show()

    # ── right-side summary panel ──

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
        self._payees_value = QLabel()
        tokens.themed(self._payees_value, "color: {muted_strong};")
        self._top_value = QLabel()
        self._top_value.setWordWrap(True)
        tokens.themed(self._top_value, "color: {muted_strong};")
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
        layout.addWidget(self._payees_value)
        layout.addWidget(self._top_value)
        layout.addSpacing(6)
        layout.addWidget(self._note_value)
        layout.addStretch(1)
        return panel

    @staticmethod
    def _mini_section_title(text: str) -> QLabel:
        lab = QLabel(text.upper())
        tokens.themed(lab, "color: {subtle}; font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        return lab

    # ── refresh / render ──

    def _refresh(self) -> None:
        filters = self._current_filters
        d_from, d_to = self._resolve_date_bounds(filters)

        account_ids = list(filters.account_ids) or [a.id for a in self._all_accounts]
        if not account_ids:
            self._show_empty("Select at least one account.")
            return

        result = self._repo.payee_spending_aggregates(
            date_from=d_from.isoformat(),
            date_to=d_to.isoformat(),
            account_ids=account_ids,
            display_currency=self._display_ccy,
            include_transfers=filters.include_transfers,
        )
        report = build_report(result["payees"], filters.top_n)
        self._render(report, d_from, d_to, filters, result.get("unconverted", {}))

    def _render(
        self, report, d_from: date, d_to: date,
        filters: PayeeReportFilters, unconverted: dict,
    ) -> None:
        if not report.rows:
            self._show_empty("No spending in the selected range / filters.")
            return
        symbol = _symbol_for(self._display_ccy) or ""
        self._chart.render(rows=report.rows, symbol=symbol or "£")
        self._populate_table(report.rows, symbol)
        self._update_summary_panel(
            filters=filters, d_from=d_from, d_to=d_to,
            summary=report.summary, unconverted=unconverted,
        )

    def _show_empty(self, message: str) -> None:
        self._chart.show_empty(message)
        self._populate_table([], _symbol_for(self._display_ccy))
        self._update_summary_panel(
            filters=self._current_filters, d_from=None, d_to=None,
            summary=None, unconverted={}, note=message,
        )

    def _fmt(self, value, decimals: int = 2) -> str:
        symbol = _symbol_for(self._display_ccy) or ""
        prefix = symbol if symbol else f"{self._display_ccy} "
        return fmt_currency(float(value), decimals, symbol=prefix)

    def _update_summary_panel(
        self,
        *,
        filters: PayeeReportFilters,
        d_from: Optional[date],
        d_to: Optional[date],
        summary,
        unconverted: dict,
        note: Optional[str] = None,
    ) -> None:
        period_label = _PERIOD_LABELS.get(filters.period_key, filters.period_key)
        if d_from is not None and d_to is not None:
            self._period_value.setText(
                f"{period_label}\n{d_from.isoformat()} → {d_to.isoformat()}"
            )
        else:
            self._period_value.setText(period_label)

        top_n_bit = (
            "Top: all payees" if filters.top_n <= 0
            else f"Top: {filters.top_n} payees"
        )
        filter_bits = [
            self._filter_line(
                "Accounts", filters.account_ids, len(self._all_accounts),
            ),
            top_n_bit,
            "Transfers: " + (
                "included" if filters.include_transfers else "excluded"
            ),
        ]
        self._filters_value.setText("\n".join(filter_bits))

        if summary is None:
            self._total_value.setText("—")
            self._payees_value.setText(note or "")
            self._top_value.setText("")
        else:
            self._total_value.setText(f"Total: {self._fmt(summary.total)}")
            payees_word = "payee" if summary.payee_count == 1 else "payees"
            if summary.hidden_count > 0:
                self._payees_value.setText(
                    f"Showing top {summary.shown_count} of "
                    f"{summary.payee_count} {payees_word} "
                    f"({summary.hidden_count} hidden)"
                )
            else:
                self._payees_value.setText(
                    f"{summary.payee_count} {payees_word}"
                )
            if summary.top_name:
                self._top_value.setText(
                    f"Top: {summary.top_name}\n{self._fmt(summary.top_amount)}"
                )
            else:
                self._top_value.setText("")

        # Excluded-currency note (ADR-055 — no-rate amounts dropped, not
        # par-added).
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

    def _resolve_date_bounds(
        self, filters: PayeeReportFilters,
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
        dialog = PayeeFilterDialog(
            self._repo,
            current=self._current_filters,
            accounts=self._all_accounts,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
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

    def _filters_to_persist(self) -> "PayeeReportFilters":
        """The current filters with the live splitter sizes folded in, so a
        layout the owner tuned is saved with the report (ADR-076 follow-up)."""
        from dataclasses import replace
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
            row = self._repo.create_report(
                name=choice.name,
                type_key=TYPE_PAYEE,
                folder_id=choice.folder_id,
                filters_json=self._filters_to_persist().to_json(),
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
        self._report_id = row.id
        self._loaded_name = row.name
        self._loaded_folder_id = row.folder_id
        self._dirty = False
        self._update_name_label()
        self._update_save_buttons()
        self.reports_changed.emit()

    def _update_name_label(self) -> None:
        if self._loaded_name is None:
            self._name_label.setText("Untitled Payee report")
            tokens.themed(self._name_label, "color: {muted}; font-style: italic; font-weight: bold; padding: 4px 8px;")
            self.setWindowTitle("Payee — Untitled")
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
        self.setWindowTitle(
            f"Payee — {prefix}{self._loaded_name}{dirty_mark}"
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
