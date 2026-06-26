"""Sankey report — income → Total → expenses (ADR-056).

A cash-flow flow diagram: income categories on the left merge into a central
Total, which fans out to expense categories (nested by hierarchy) plus a
Savings node. Income vs expense is read from ``category.kind`` (transfers
excluded), period-scoped by the timeframe control.

Inline controls (no modal — the owner wants to toggle quickly): timeframe,
how many category levels to expand, a threshold that folds small slices into
"Other", and an amounts/% switch. A right-hand panel summarises income,
expenditure, amount saved, and saving %. Save / Save As persist the control
state as a report row (ADR-039 framework), type ``sankey``.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
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
from PySide6.QtGui import QColor

from mfl_desktop.db.repository import Repository, ReportRow
from mfl_desktop.reports.filters import (
    SankeyFilters, TYPE_SANKEY,
)
from mfl_desktop.ui.chart_helpers import colour_for, fmt_currency
from mfl_desktop.ui.custom_period_dialog import CustomPeriodDialog
from mfl_desktop.ui.sankey_chart import SankeyChart, SankeyNode
from mfl_desktop.ui.sankey_filter_dialog import SankeyFilterDialog
from mfl_desktop.ui.save_report_as_dialog import SaveReportAsDialog
from mfl_desktop.ui.transactions_list_window import (
    TransactionsListWindow, TxnListFilter,
)
from mfl_desktop.ui import tokens
from mfl_desktop.ui.report_save import resolve_save_as
from mfl_desktop import periods
from dataclasses import replace

_DEPTH_LABELS = [
    ("Top level", 1),
    ("2 levels", 2),
    ("3 levels", 3),
    ("4 levels", 4),
]
_OTHER_COLOR = QColor("#cbd5e1")
_SAVINGS_COLOR = QColor("#16a34a")
_DEFICIT_COLOR = QColor("#dc2626")

_CCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥"}


def _symbol_for(currency: str) -> str:
    return _CCY_SYMBOLS.get((currency or "").upper(), "")


class SankeyReportWindow(QMainWindow):
    reports_changed = Signal()

    def __init__(
        self, repo: Repository, *,
        report: Optional[ReportRow] = None, parent=None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._report_id = report.id if report is not None else None
        self._loaded_name = report.name if report is not None else None
        self._loaded_folder_id = report.folder_id if report is not None else None
        self._dirty = False
        self._display_ccy = "GBP"
        self.resize(1280, 760)

        self._current_filters: SankeyFilters = (
            SankeyFilters.from_json(report.filters_json)
            if report is not None else SankeyFilters.default()
        )

        # Category tree (id → node) + parent→children, loaded once.
        nodes = self._repo.list_category_tree()
        self._cat_nodes = nodes
        self._cat = {n.id: n for n in nodes}
        self._children: dict[int, list[int]] = {}
        for n in nodes:
            if n.parent_id is not None:
                self._children.setdefault(n.parent_id, []).append(n.id)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_top_bar())
        outer.addWidget(self._build_controls())
        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        tokens.themed(rule, "color: {border};")
        outer.addWidget(rule)

        self._chart = SankeyChart()
        self._chart.node_clicked.connect(self._on_node_clicked)
        self._summary_panel = self._build_summary_panel()
        body = QSplitter(Qt.Horizontal)
        self._body_splitter = body
        body.addWidget(self._chart)
        body.addWidget(self._summary_panel)
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 0)
        _bs = self._current_filters.body_split
        body.setSizes(list(_bs) if _bs else [980, 300])
        body.splitterMoved.connect(lambda *_: self._mark_dirty())
        outer.addWidget(body, 1)
        self.setCentralWidget(central)

        self._sync_controls_from_filters()
        self._update_name_label()
        self._update_save_buttons()
        self._refresh()

    # ── constructors ──

    @classmethod
    def open_bare(cls, repo: Repository, parent=None) -> "SankeyReportWindow":
        return cls(repo, report=None, parent=parent)

    @classmethod
    def load_from_id(
        cls, repo: Repository, report_id: int, parent=None,
    ) -> Optional["SankeyReportWindow"]:
        report = repo.get_report(report_id)
        if report is None or report.type != TYPE_SANKEY:
            return None
        return cls(repo, report=report, parent=parent)

    # ── build ──

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        self._name_label = QLabel()
        tokens.themed(self._name_label, "color: {heading}; font-weight: bold;")
        row.addWidget(self._name_label)
        row.addStretch(1)
        row.addWidget(QLabel("Display in:"))
        self._ccy_combo = QComboBox()
        self._ccy_combo.currentIndexChanged.connect(self._on_ccy_changed)
        row.addWidget(self._ccy_combo)
        self._populate_ccy_combo()
        self._save_button = QPushButton("Save")
        self._save_button.clicked.connect(self._on_save)
        self._save_as_button = QPushButton("Save As…")
        self._save_as_button.clicked.connect(self._on_save_as)
        row.addWidget(self._save_button)
        row.addWidget(self._save_as_button)
        return bar

    def _build_controls(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 4, 12, 8)
        row.setSpacing(10)

        # Timeframe presets sourced from the shared vocabulary (ADR-082); the
        # Sankey set now includes Last 6/12 months alongside its cash-flow-native
        # MTD / Last month. "Custom" keeps its … affordance (opens a dialog).
        self._period_combo = QComboBox()
        for label, key in periods.options_for(periods.SANKEY_PRESETS):
            self._period_combo.addItem(label + ("…" if key == "custom" else ""), key)
        self._period_combo.currentIndexChanged.connect(self._on_period_changed)
        row.addWidget(QLabel("Timeframe:"))
        row.addWidget(self._period_combo)

        self._depth_combo = QComboBox()
        for label, d in _DEPTH_LABELS:
            self._depth_combo.addItem(label, d)
        self._depth_combo.currentIndexChanged.connect(self._on_control_changed)
        row.addWidget(QLabel("Levels:"))
        row.addWidget(self._depth_combo)

        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(0.0, 50.0)
        self._threshold_spin.setSingleStep(1.0)
        self._threshold_spin.setDecimals(1)
        self._threshold_spin.setSuffix(" %")
        self._threshold_spin.setToolTip(
            "Fold any slice smaller than this share of the side's total into an "
            "'Other' node. 0 shows everything."
        )
        self._threshold_spin.valueChanged.connect(self._on_control_changed)
        row.addWidget(QLabel("Hide below:"))
        row.addWidget(self._threshold_spin)

        self._value_combo = QComboBox()
        self._value_combo.addItem("Amounts", "amount")
        self._value_combo.addItem("Percent", "percent")
        self._value_combo.currentIndexChanged.connect(self._on_control_changed)
        row.addWidget(QLabel("Show:"))
        row.addWidget(self._value_combo)

        self._filter_button = QPushButton("Filter…")
        self._filter_button.clicked.connect(self._on_filter)
        row.addWidget(self._filter_button)
        self._filter_note = QLabel("")
        tokens.themed(self._filter_note, "color: {accent};")
        row.addWidget(self._filter_note)

        row.addStretch(1)
        self._period_note = QLabel("")
        tokens.themed(self._period_note, "color: {muted};")
        row.addWidget(self._period_note)
        return bar

    def _build_summary_panel(self) -> QWidget:
        panel = QFrame()
        tokens.themed(panel, "QFrame { background: {canvas}; border-left: 1px solid {border}; }QLabel { background: transparent; }")
        panel.setMinimumWidth(260)
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(6)

        def title(t: str) -> QLabel:
            lab = QLabel(t.upper())
            tokens.themed(lab, "color: {subtle}; font-size: 10px; font-weight: bold; letter-spacing: 1px;")
            return lab

        v.addWidget(title("Income"))
        self._income_value = QLabel("—")
        tokens.themed(self._income_value, "color: {positive}; font-size: 20px; font-weight: bold;")
        v.addWidget(self._income_value)
        v.addSpacing(4)

        v.addWidget(title("Expenditure"))
        self._expense_value = QLabel("—")
        tokens.themed(self._expense_value, "color: {negative}; font-size: 20px; font-weight: bold;")
        v.addWidget(self._expense_value)
        v.addSpacing(4)

        v.addWidget(title("Amount saved"))
        self._saved_value = QLabel("—")
        tokens.themed(self._saved_value, "color: {text}; font-size: 20px; font-weight: bold;")
        v.addWidget(self._saved_value)
        self._saving_rate = QLabel("")
        tokens.themed(self._saving_rate, "color: {muted_strong};")
        v.addWidget(self._saving_rate)

        v.addSpacing(10)
        self._note = QLabel("")
        self._note.setWordWrap(True)
        tokens.themed(self._note, "color: {warning}; font-style: italic;")
        v.addWidget(self._note)
        v.addStretch(1)
        return panel

    def _populate_ccy_combo(self) -> None:
        """Fill the display-currency selector from the currencies in use,
        defaulting to the base currency (then GBP, then the first in use).
        Like Net Worth (ADR-055), this is a view preference — not persisted in
        the saved filters; it re-resolves to the default each time the report
        opens."""
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

    # ── control sync ──

    def _sync_controls_from_filters(self) -> None:
        f = self._current_filters
        for combo, value in (
            (self._period_combo, f.period_key),
            (self._depth_combo, f.depth),
            (self._value_combo, f.value_mode),
        ):
            i = combo.findData(value)
            combo.blockSignals(True)
            combo.setCurrentIndex(i if i >= 0 else 0)
            combo.blockSignals(False)
        self._threshold_spin.blockSignals(True)
        self._threshold_spin.setValue(f.threshold_pct)
        self._threshold_spin.blockSignals(False)

    def _with(self, **changes) -> SankeyFilters:
        f = self._current_filters
        return SankeyFilters(
            period_key=changes.get("period_key", f.period_key),
            custom_start=changes.get("custom_start", f.custom_start),
            custom_end=changes.get("custom_end", f.custom_end),
            depth=changes.get("depth", f.depth),
            threshold_pct=changes.get("threshold_pct", f.threshold_pct),
            value_mode=changes.get("value_mode", f.value_mode),
            account_ids=changes.get("account_ids", f.account_ids),
            category_ids=changes.get("category_ids", f.category_ids),
        )

    def _on_control_changed(self, *_a) -> None:
        self._current_filters = self._with(
            depth=self._depth_combo.currentData(),
            threshold_pct=self._threshold_spin.value(),
            value_mode=self._value_combo.currentData(),
        )
        self._mark_dirty()
        self._refresh()

    def _on_period_changed(self, *_a) -> None:
        key = self._period_combo.currentData()
        if key == "custom":
            d_from, d_to = self._resolve_bounds()
            dlg = CustomPeriodDialog(initial_from=d_from, initial_to=d_to, parent=self)
            if dlg.exec() != QDialog.Accepted:
                # Revert the combo to the prior selection.
                self._sync_controls_from_filters()
                return
            s, e = dlg.values()
            self._current_filters = self._with(
                period_key="custom",
                custom_start=s.isoformat(), custom_end=e.isoformat(),
            )
        else:
            self._current_filters = self._with(period_key=key)
        self._mark_dirty()
        self._refresh()

    def _on_filter(self, *_a) -> None:
        f = self._current_filters
        dlg = SankeyFilterDialog(
            self._repo,
            # Reports include closed accounts by default (ADR-115).
            accounts=self._repo.list_accounts(include_closed=True),
            categories=self._cat_nodes,
            current_account_ids=f.account_ids,
            current_category_ids=f.category_ids,
            parent=self,
        )
        accepted = dlg.exec() == QDialog.Accepted
        # ADR-105: keep this report in front after the modal filter closes.
        self.raise_()
        self.activateWindow()
        if not accepted:
            return
        chosen = dlg.values()
        if chosen is None:
            return
        account_ids, category_ids = chosen
        self._current_filters = self._with(
            account_ids=account_ids, category_ids=category_ids,
        )
        self._mark_dirty()
        self._refresh()

    # ── data ──

    def _resolve_bounds(self) -> tuple[date, date]:
        f = self._current_filters
        today = date.today()
        if f.period_key == "mtd":
            return date(today.year, today.month, 1), today
        if f.period_key == "last_month":
            first_this = date(today.year, today.month, 1)
            last_prev = first_this - timedelta(days=1)
            return date(last_prev.year, last_prev.month, 1), last_prev
        if f.period_key == "custom":
            # Partial custom bounds fall back to YTD-start / today (unchanged).
            s = date.fromisoformat(f.custom_start) if f.custom_start else date(today.year, 1, 1)
            e = date.fromisoformat(f.custom_end) if f.custom_end else today
            return s, e
        start, end = periods.period_bounds(f.period_key, today)  # ytd / mtd / last_month
        return start, end

    def _on_node_clicked(self, category_id: int, label: str) -> None:
        """Drill a Sankey category node to its transactions (ADR-083) — that
        category and its descendants over the report's period and account
        scope. A single selected account drills per-account; 0 / a subset
        opens the cross-account view (mirrors the Payee report)."""
        d_from, d_to = self._resolve_bounds()
        acc_ids = list(self._current_filters.account_ids)
        if len(acc_ids) == 1:
            account_id: Optional[int] = acc_ids[0]
            account_name = next(
                (a.name for a in self._repo.list_accounts(include_closed=True)
                 if a.id == account_id),
                "",
            )
        else:
            account_id, account_name = None, ""
        flt = TxnListFilter.for_category(
            account_id=account_id, account_name=account_name,
            category_id=category_id, category_label=label,
            period_key="custom", custom_start=d_from, custom_end=d_to,
        )
        win = TransactionsListWindow(self._repo, flt, parent=self)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.show()

    def _rolled_pence(self, cid: int, aggregate: dict[int, int]) -> int:
        """Category value rolled up its subtree: own line total + every
        descendant's. The aggregate only holds same-kind leaves, so transfer /
        other-kind descendants contribute nothing."""
        total = aggregate.get(cid, 0)
        for ch in self._children.get(cid, []):
            total += self._rolled_pence(ch, aggregate)
        return total

    def _build_side(
        self, kind: str, aggregate: dict[int, int], side_total_pence: int,
        depth: int, threshold_pct: float,
    ) -> list[SankeyNode]:
        """Build the node tree for one side. Roots are categories of ``kind``
        whose parent isn't the same kind (the top of each subtree). Small nodes
        (< threshold % of the side total) fold into an 'Other' sibling."""
        roots = [
            cid for cid, n in self._cat.items()
            if n.kind == kind
            and (n.parent_id is None or self._cat[self._cat[cid].parent_id].kind != kind)
        ]
        thresh = side_total_pence * threshold_pct / 100.0

        def make(cids: list[int], depth_remaining: int, colour) -> list[SankeyNode]:
            entries = [(c, self._rolled_pence(c, aggregate)) for c in cids]
            entries = [(c, v) for c, v in entries if v > 0]
            big = [(c, v) for c, v in entries if v >= thresh]
            small_total = sum(v for c, v in entries if v < thresh)
            out: list[SankeyNode] = []
            for idx, (c, v) in enumerate(sorted(big, key=lambda x: -x[1])):
                col = colour(idx)
                node = SankeyNode(
                    label=self._cat[c].name, value=v / 100.0, color=col,
                    category_id=c,
                )
                if depth_remaining > 1:
                    kids = [
                        k for k in self._children.get(c, [])
                        if self._cat[k].kind == kind
                    ]
                    node.children = make(kids, depth_remaining - 1, lambda _i: col)
                out.append(node)
            if small_total > 0:
                out.append(SankeyNode(
                    label="Other", value=small_total / 100.0,
                    color=_OTHER_COLOR, is_other=True,
                ))
            return out

        return make(roots, depth, lambda i: colour_for(i))

    def _refresh(self) -> None:
        f = self._current_filters
        d_from, d_to = self._resolve_bounds()
        self._period_note.setText(f"{d_from.isoformat()} → {d_to.isoformat()}")
        self._update_filter_note()
        totals = self._repo.sankey_category_totals(
            date_from=d_from.isoformat(), date_to=d_to.isoformat(),
            account_ids=f.account_ids, category_ids=f.category_ids,
            display_currency=self._display_ccy,
        )
        income_agg, expense_agg = totals["income"], totals["expense"]
        self._unconverted = totals.get("unconverted", {})
        income_pence = sum(income_agg.values())
        expense_pence = sum(expense_agg.values())

        income_nodes = self._build_side(
            "income", income_agg, income_pence, f.depth, f.threshold_pct,
        )
        expense_nodes = self._build_side(
            "expense", expense_agg, expense_pence, f.depth, f.threshold_pct,
        )

        # Balance the shorter side so both fill the spine: a Savings node when
        # income > expense, a Deficit node when expense > income.
        saved_pence = income_pence - expense_pence
        if saved_pence > 0:
            expense_nodes.append(SankeyNode(
                label="Savings", value=saved_pence / 100.0,
                color=_SAVINGS_COLOR, is_balance=True,
            ))
        elif saved_pence < 0:
            income_nodes.append(SankeyNode(
                label="Deficit", value=(-saved_pence) / 100.0,
                color=_DEFICIT_COLOR, is_balance=True,
            ))

        self._update_summary(income_pence, expense_pence, saved_pence)

        if income_pence <= 0 and expense_pence <= 0:
            self._chart.show_empty(
                "No income or expense in this period.\n"
                "Tip: income vs expense is read from each category's kind "
                "(Manage ▸ Categories)."
            )
            return
        self._chart.render(
            income=income_nodes, expense=expense_nodes,
            total_income=income_pence / 100.0,
            total_expense=expense_pence / 100.0,
            value_mode=f.value_mode,
            currency_symbol=_symbol_for(self._display_ccy) or self._display_ccy + " ",
        )

    def _update_summary(self, income_p: int, expense_p: int, saved_p: int) -> None:
        sym = _symbol_for(self._display_ccy) or self._display_ccy + " "
        self._income_value.setText(fmt_currency(income_p / 100.0, symbol=sym))
        self._expense_value.setText(fmt_currency(expense_p / 100.0, symbol=sym))
        sign = "-" if saved_p < 0 else ""
        self._saved_value.setText(
            f"{sign}{fmt_currency(abs(saved_p) / 100.0, symbol=sym)}"
        )
        self._saved_value.setStyleSheet(
            "font-size: 20px; font-weight: bold; color: "
            + ("#16a34a" if saved_p >= 0 else "#dc2626") + ";"
        )
        if income_p > 0:
            rate = saved_p / income_p * 100.0
            self._saving_rate.setText(
                f"Saving rate: {rate:.1f}% of income"
                if saved_p >= 0 else f"Overspend: {-rate:.1f}% of income"
            )
        else:
            self._saving_rate.setText("")
        # Priority: excluded (no-rate) amounts, then the kinds-setup hint.
        unconverted = getattr(self, "_unconverted", {})
        if unconverted:
            bits = ", ".join(
                f"{_symbol_for(c) or c + ' '}{p / 100.0:,.0f} {c}"
                for c, p in sorted(unconverted.items())
            )
            self._note.setText(
                f"Excluded (no rate to {self._display_ccy}): {bits}. "
                f"Set rates in Manage ▸ Currencies."
            )
        else:
            # Income comes from category.kind='income'; flag the common gap.
            self._note.setText(
                "No income categories found — set category kinds in Manage ▸ "
                "Categories so income appears on the left."
                if income_p <= 0 and expense_p > 0 else ""
            )

    def _update_filter_note(self) -> None:
        f = self._current_filters
        parts: list[str] = []
        if f.account_ids:
            n = len(f.account_ids)
            parts.append(f"{n} account{'s' if n != 1 else ''}")
        if f.category_ids:
            n = len(f.category_ids)
            parts.append(f"{n} categor{'ies' if n != 1 else 'y'}")
        self._filter_note.setText("Filtered: " + ", ".join(parts) if parts else "")

    # ── save / dirty ──

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_name_label()

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
        self._repo.update_report(
            self._report_id, filters_json=self._filters_to_persist().to_json(),
        )
        self._dirty = False
        self._update_name_label()
        self.reports_changed.emit()

    def _on_save_as(self) -> None:
        dlg = SaveReportAsDialog(
            self._repo, initial_name=self._loaded_name or "Sankey", parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        choice = dlg.values()
        if choice is None:
            return
        try:
            row = resolve_save_as(
                self, self._repo, self._report_id, TYPE_SANKEY,
                choice.name, choice.folder_id, self._filters_to_persist().to_json(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not save report", str(e))
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
        name = self._loaded_name or "Sankey (unsaved)"
        self._name_label.setText(name + ("  •" if self._dirty else ""))

    def _update_save_buttons(self) -> None:
        bare = self._report_id is None
        self._save_button.setVisible(not bare)
