"""Investment Returns — total-return report (ADR-046).

Non-modal QMainWindow that shows, for a single investment account *or* the
whole portfolio, how total return breaks down into cost / unrealized gain /
realized gain / dividends — both visually (a stacked-composition chart over
time, :class:`ReturnsChart`) and numerically (portfolio totals + a per-security
breakdown table). Filters (period, accounts, securities) live in a modal
:class:`InvestmentReturnsFilterDialog` opened from the top bar.

Realized gains and dividends are period-scoped: they count only when the sale /
distribution falls inside the selected window (a position sold years ago shows
nothing in a YTD view). Unrealized gain is the lifetime gain of currently-held
shares. See ADR-046.

Saved/loaded through the ADR-039 reports framework — structurally the same
top-bar / Save / Save As / dirty / close-prompt scaffolding as
SpendingReportWindow, minus the drill-down (returns has no drill stack).

Currency: when all selected accounts share a currency the report aggregates
natively; a mixed-currency selection converts each account into the first
account's currency via Repository.convert_amount (a note flags it). The owner's
portfolio is single-currency USD, so native is the live path.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
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

from mfl_desktop.db.repository import Repository, ReportRow
from mfl_desktop.holdings import ReturnPoint, compute_returns
from mfl_desktop.reports.filters import (
    InvestmentReturnsFilters, TYPE_INVESTMENT_RETURNS,
)
from mfl_desktop.ui.investment_returns_filter_dialog import (
    InvestmentReturnsFilterDialog,
)
from mfl_desktop.ui.returns_chart import ReturnsChart
from mfl_desktop.ui.save_report_as_dialog import SaveReportAsDialog

_CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥"}

_PERIOD_LABELS: dict[str, str] = {
    "ytd":    "Year to date",
    "1y":     "Last 12 months",
    "3y":     "Last 3 years",
    "5y":     "Last 5 years",
    "max":    "Max (all history)",
    "custom": "Custom",
}

_GAIN = "#16a34a"
_LOSS = "#dc2626"

_TABLE_HEADERS = (
    "Symbol", "Security", "Cost", "Market value",
    "Unrealized", "Realized", "Dividends", "Total return", "Return %",
)


def _sym(currency: Optional[str]) -> str:
    return _CURRENCY_SYMBOLS.get((currency or "").upper(), "")


def _month_end_samples(start: date, end: date) -> list[date]:
    """Sample dates spanning ``[start, end]``: the start, every month-end
    strictly between, and the end. Drives the chart x-axis."""
    if end <= start:
        return [start, end] if end != start else [start]
    out: list[date] = [start]
    y, m = start.year, start.month
    while True:
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        eom = date(ny, nm, 1) - timedelta(days=1)
        if eom >= end:
            break
        if eom > start:
            out.append(eom)
        y, m = ny, nm
    out.append(end)
    return out


class InvestmentReturnsWindow(QMainWindow):
    """Investment Returns report window — bare or saved-loaded."""

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
        self.resize(1240, 760)

        # reference data
        self._all_accounts = repo.list_investment_accounts()
        self._accounts_by_id = {a.id: a for a in self._all_accounts}

        self._current_filters: InvestmentReturnsFilters = (
            InvestmentReturnsFilters.from_json(report.filters_json)
            if report is not None
            else InvestmentReturnsFilters.default()
        )

        # display currency / conversion flags, set per refresh
        self._display_ccy = ""
        self._convert_missing = False
        self._convert_fallback = False

        # ── top bar ──
        self._name_label = QLabel()
        self._name_label.setStyleSheet(
            "color: #334155; font-weight: bold; padding: 4px 8px;"
        )
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
        top_bar_layout.addWidget(self._filter_button)
        top_bar_layout.addWidget(self._save_button)
        top_bar_layout.addWidget(self._save_as_button)

        top_rule = QFrame()
        top_rule.setFrameShape(QFrame.HLine)
        top_rule.setFrameShadow(QFrame.Sunken)
        top_rule.setStyleSheet("color: #e2e8f0;")

        # ── body: (chart over table) | summary ──
        self._chart = ReturnsChart()

        self._table = QTableWidget(0, len(_TABLE_HEADERS))
        self._table.setHorizontalHeaderLabels(_TABLE_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setAlternatingRowColors(True)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in range(2, len(_TABLE_HEADERS)):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        left_splitter = QSplitter(Qt.Vertical)
        left_splitter.addWidget(self._chart)
        left_splitter.addWidget(self._table)
        left_splitter.setStretchFactor(0, 1)
        left_splitter.setStretchFactor(1, 0)
        left_splitter.setSizes([460, 280])

        self._summary_panel = self._build_summary_panel()

        body_splitter = QSplitter(Qt.Horizontal)
        body_splitter.addWidget(left_splitter)
        body_splitter.addWidget(self._summary_panel)
        body_splitter.setStretchFactor(0, 1)
        body_splitter.setStretchFactor(1, 0)
        body_splitter.setSizes([940, 300])

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(top_bar)
        central_layout.addWidget(top_rule)
        central_layout.addWidget(body_splitter, stretch=1)
        self.setCentralWidget(central)

        self._update_name_label()
        self._update_save_buttons()
        self._refresh()

    # ── constructors ──

    @classmethod
    def open_bare(cls, repo: Repository, parent=None) -> "InvestmentReturnsWindow":
        return cls(repo, report=None, parent=parent)

    @classmethod
    def load_from_id(
        cls, repo: Repository, report_id: int, parent=None,
    ) -> Optional["InvestmentReturnsWindow"]:
        report = repo.get_report(report_id)
        if report is None or report.type != TYPE_INVESTMENT_RETURNS:
            return None
        return cls(repo, report=report, parent=parent)

    # ── summary panel ──

    def _build_summary_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.NoFrame)
        panel.setStyleSheet(
            "QFrame { background: #f8fafc; border-left: 1px solid #e2e8f0; }"
            "QLabel { background: transparent; }"
        )
        panel.setMinimumWidth(260)

        self._period_value = QLabel()
        self._period_value.setWordWrap(True)
        self._period_value.setStyleSheet("color: #0f172a;")
        self._filters_value = QLabel()
        self._filters_value.setWordWrap(True)
        self._filters_value.setStyleSheet("color: #475569;")

        self._cost_value = QLabel()
        self._market_value = QLabel()
        self._unrealized_value = QLabel()
        self._realized_value = QLabel()
        self._dividends_value = QLabel()
        for lab in (self._cost_value, self._market_value, self._unrealized_value,
                    self._realized_value, self._dividends_value):
            lab.setStyleSheet("color: #0f172a;")

        self._total_value = QLabel()
        self._total_value.setStyleSheet(
            "color: #0f172a; font-size: 22px; font-weight: bold;"
        )
        self._roi_value = QLabel()
        self._roi_value.setStyleSheet("color: #475569;")
        self._note_value = QLabel()
        self._note_value.setWordWrap(True)
        self._note_value.setStyleSheet("color: #b45309; font-style: italic;")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        layout.addWidget(self._mini_section_title("Period"))
        layout.addWidget(self._period_value)
        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Filters"))
        layout.addWidget(self._filters_value)
        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Totals"))
        layout.addWidget(self._cost_value)
        layout.addWidget(self._market_value)
        layout.addWidget(self._unrealized_value)
        layout.addWidget(self._realized_value)
        layout.addWidget(self._dividends_value)
        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Total return"))
        layout.addWidget(self._total_value)
        layout.addWidget(self._roi_value)
        layout.addSpacing(6)
        layout.addWidget(self._note_value)
        layout.addStretch(1)
        return panel

    @staticmethod
    def _mini_section_title(text: str) -> QLabel:
        lab = QLabel(text.upper())
        lab.setStyleSheet(
            "color: #94a3b8; font-size: 10px; font-weight: bold; "
            "letter-spacing: 1px;"
        )
        return lab

    # ── currency formatting ──

    def _money(self, amount: Decimal | float, decimals: int = 2) -> str:
        sym = _sym(self._display_ccy)
        a = float(amount)
        sign = "-" if a < 0 else ""
        return f"{sign}{sym}{abs(a):,.{decimals}f}"

    def _signed(self, amount: Decimal | float, decimals: int = 2) -> str:
        a = float(amount)
        sign = "+" if a >= 0 else "-"
        sym = _sym(self._display_ccy)
        return f"{sign}{sym}{abs(a):,.{decimals}f}"

    @staticmethod
    def _pct(numer: float, denom: float) -> str:
        if not denom:
            return ""
        p = numer / denom * 100
        sign = "+" if p >= 0 else "-"
        return f" ({sign}{abs(p):.1f}%)"

    @staticmethod
    def _roi(numer: float, denom: float) -> str:
        """Return-on-cost as a standalone percent (no surrounding parens).
        Blank when there's no cost deployed (denom 0)."""
        if not denom:
            return "—"
        p = numer / denom * 100
        sign = "+" if p >= 0 else "-"
        return f"{sign}{abs(p):.1f}%"

    # ── period resolution + conversion ──

    def _resolve_bounds(self, earliest: Optional[date]) -> tuple[date, date]:
        f = self._current_filters
        today = date.today()
        key = f.period_key
        if key == "custom" and f.custom_start and f.custom_end:
            try:
                a = date.fromisoformat(f.custom_start)
                b = date.fromisoformat(f.custom_end)
                return (a, b) if a <= b else (b, a)
            except ValueError:
                pass
        if key == "ytd":
            return date(today.year, 1, 1), today
        if key == "1y":
            return today - timedelta(days=365), today
        if key == "3y":
            return today - timedelta(days=3 * 365), today
        if key == "5y":
            return today - timedelta(days=5 * 365), today
        # "max" (and any fallback): first transaction → today.
        return (earliest or today), today

    def _conv(self, amount: Decimal, from_ccy: str, on_date: str) -> Decimal:
        if from_ccy == self._display_ccy:
            return amount
        converted, fallback = self._repo.convert_amount(
            amount, from_ccy=from_ccy, to_ccy=self._display_ccy, on_date=on_date,
        )
        if converted is None:
            self._convert_missing = True
            return amount  # 1:1 fallback so the report still renders
        if fallback:
            self._convert_fallback = True
        return converted

    # ── refresh / render ──

    def _refresh(self) -> None:
        self._convert_missing = False
        self._convert_fallback = False
        f = self._current_filters

        if not self._all_accounts:
            self._display_ccy = ""
            self._show_empty("No investment accounts yet.")
            return

        account_ids = list(f.account_ids) or [a.id for a in self._all_accounts]
        accounts = [self._accounts_by_id[i] for i in account_ids
                    if i in self._accounts_by_id]
        if not accounts:
            self._show_empty("Select at least one account.")
            return

        security_ids: Optional[set[int]] = set(f.security_ids) or None

        # Per-account full-history txns + earliest date.
        per_account: list[tuple] = []   # (account, txns, price_series)
        earliest: Optional[date] = None
        for acct in accounts:
            txns = self._repo.list_transactions_for_account(acct.id)
            dated = [t.posted_date for t in txns if t.posted_date]
            if dated:
                first = date.fromisoformat(min(dated))
                earliest = first if earliest is None else min(earliest, first)
            sec_ids = {t.security_id for t in txns if t.security_id is not None}
            pser = {
                sid: [(p.price_date, p.price) for p in self._repo.price_series(sid)]
                for sid in sec_ids
            }
            per_account.append((acct, txns, pser))

        d_from, d_to = self._resolve_bounds(earliest)
        samples = _month_end_samples(d_from, d_to)
        samples_iso = sorted({d.isoformat() for d in samples})
        window_start = d_from.isoformat()
        end_iso = d_to.isoformat()

        # Display currency: native if uniform, else first account's currency.
        currencies = {a.currency for a, _, _ in per_account}
        self._display_ccy = (
            next(iter(currencies)) if len(currencies) == 1
            else accounts[0].currency
        )

        results = [
            (acct, compute_returns(txns, samples, pser, window_start, security_ids))
            for acct, txns, pser in per_account
        ]

        # Aggregate points by sample index (all results share samples_iso).
        n = len(samples_iso)
        agg_points: list[ReturnPoint] = []
        any_fallback = False
        for i in range(n):
            date_i = samples_iso[i]
            cost = mv = realized = div = Decimal("0")
            fully = True
            for acct, res in results:
                if i >= len(res.points):
                    continue
                p = res.points[i]
                cost += self._conv(p.cost_basis, acct.currency, date_i)
                mv += self._conv(p.market_value, acct.currency, date_i)
                realized += self._conv(p.realized_cum, acct.currency, date_i)
                div += self._conv(p.dividends_cum, acct.currency, date_i)
                fully = fully and p.fully_priced
            any_fallback = any_fallback or not fully
            agg_points.append(ReturnPoint(
                date=date_i, cost_basis=cost, market_value=mv,
                unrealized=mv - cost, realized_cum=realized,
                dividends_cum=div, fully_priced=fully,
            ))

        # Aggregate per-security at end-of-window (convert at end date).
        merged: dict[int, dict] = {}
        for acct, res in results:
            for s in res.by_security:
                m = merged.setdefault(s.security_id, {
                    "symbol": s.symbol, "name": s.name, "shares": 0.0,
                    "cost": Decimal("0"), "cost_sold": Decimal("0"),
                    "mv": Decimal("0"),
                    "unreal": Decimal("0"), "realized": Decimal("0"),
                    "div": Decimal("0"), "priced": False,
                })
                m["shares"] += s.shares
                m["cost"] += self._conv(s.cost_basis, acct.currency, end_iso)
                m["cost_sold"] += self._conv(s.cost_basis_sold, acct.currency, end_iso)
                m["realized"] += self._conv(s.realized_window, acct.currency, end_iso)
                m["div"] += self._conv(s.dividends_window, acct.currency, end_iso)
                if s.market_value is not None:
                    m["mv"] += self._conv(s.market_value, acct.currency, end_iso)
                    m["priced"] = True
                if s.unrealized is not None:
                    m["unreal"] += self._conv(s.unrealized, acct.currency, end_iso)

        rows = list(merged.values())
        for m in rows:
            unreal = m["unreal"] if m["priced"] else Decimal("0")
            m["total"] = unreal + m["realized"] + m["div"]
            # Cost deployed = cost of shares still held + cost of shares sold in
            # the window — the capital that produced this row's return (ADR-046
            # amendment), so a fully-sold position shows its real cost, not £0.
            m["cost_total"] = m["cost"] + m["cost_sold"]
        rows.sort(key=lambda m: (
            0 if m["shares"] > 1e-9 else 1, -float(m["total"]), m["name"].lower(),
        ))

        # Portfolio totals from merged rows (consistent with the table).
        tot_cost = sum((m["cost"] for m in rows if m["shares"] > 1e-9), Decimal("0"))
        tot_cost_deployed = sum((m["cost_total"] for m in rows), Decimal("0"))
        tot_mv = sum((m["mv"] for m in rows if m["priced"]), Decimal("0"))
        tot_unreal = sum((m["unreal"] for m in rows if m["priced"]), Decimal("0"))
        tot_realized = sum((m["realized"] for m in rows), Decimal("0"))
        tot_div = sum((m["div"] for m in rows), Decimal("0"))
        tot_total = tot_unreal + tot_realized + tot_div
        unpriced = sum(1 for m in rows if m["shares"] > 1e-9 and not m["priced"])

        if len(agg_points) < 2:
            self._chart.show_empty("Not enough history to chart yet.")
        else:
            self._chart.render(agg_points, _sym(self._display_ccy), any_fallback)

        self._populate_table(rows)
        self._update_summary(
            d_from=d_from, d_to=d_to, accounts=accounts,
            security_ids=security_ids,
            cost=tot_cost, cost_deployed=tot_cost_deployed, mv=tot_mv,
            unreal=tot_unreal, realized=tot_realized, div=tot_div,
            total=tot_total, unpriced=unpriced,
        )

    def _populate_table(self, rows: list[dict]) -> None:
        self._table.setRowCount(len(rows))
        for r, m in enumerate(rows):
            priced = m["priced"]
            unreal = m["unreal"] if priced else None
            cells = [
                (m["symbol"], Qt.AlignLeft, None),
                (m["name"], Qt.AlignLeft, None),
                (self._money(m["cost_total"]), Qt.AlignRight, None),
                (self._money(m["mv"]) if priced else "—", Qt.AlignRight, None),
                (
                    self._signed(unreal) + self._pct(float(unreal), float(m["cost"]))
                    if unreal is not None else "—",
                    Qt.AlignRight,
                    self._colour(unreal),
                ),
                (self._signed(m["realized"]), Qt.AlignRight, self._colour(m["realized"])),
                (self._money(m["div"]), Qt.AlignRight, None),
                (self._signed(m["total"]), Qt.AlignRight, self._colour(m["total"])),
                (
                    self._roi(float(m["total"]), float(m["cost_total"])),
                    Qt.AlignRight,
                    self._colour(m["total"]),
                ),
            ]
            for c, (text, align, colour) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(int(align | Qt.AlignVCenter))
                if colour is not None:
                    item.setForeground(QColor(colour))
                self._table.setItem(r, c, item)

    @staticmethod
    def _colour(amount) -> Optional[str]:
        if amount is None:
            return None
        a = float(amount)
        if a > 0:
            return _GAIN
        if a < 0:
            return _LOSS
        return None

    def _update_summary(
        self, *, d_from, d_to, accounts, security_ids,
        cost, cost_deployed, mv, unreal, realized, div, total, unpriced,
    ) -> None:
        key = self._current_filters.period_key
        period_label = _PERIOD_LABELS.get(key, key)
        self._period_value.setText(
            f"{period_label}\n{d_from.isoformat()} → {d_to.isoformat()}"
        )

        if not self._current_filters.account_ids:
            acct_line = f"Accounts: all ({len(self._all_accounts)} — whole portfolio)"
        else:
            acct_line = f"Accounts: {len(accounts)} of {len(self._all_accounts)}"
        if security_ids:
            sec_line = f"Securities: {len(security_ids)} selected"
        else:
            sec_line = "Securities: all"
        ccy_line = f"Currency: {self._display_ccy}" if self._display_ccy else ""
        self._filters_value.setText("\n".join(
            x for x in (acct_line, sec_line, ccy_line) if x
        ))

        # "Cost" = capital deployed (held + sold in the window); the parenthetical
        # held figure clarifies when a position has been (partly) sold.
        if cost_deployed != cost:
            self._cost_value.setText(
                f"Cost (held + sold): {self._money(cost_deployed)}"
            )
        else:
            self._cost_value.setText(f"Cost basis: {self._money(cost_deployed)}")
        self._market_value.setText(f"Market value: {self._money(mv)}")
        self._unrealized_value.setText(
            f"Unrealized: {self._signed(unreal)}{self._pct(float(unreal), float(cost))}"
        )
        self._unrealized_value.setStyleSheet(f"color: {self._colour(unreal) or '#0f172a'};")
        self._realized_value.setText(f"Realized (in period): {self._signed(realized)}")
        self._dividends_value.setText(f"Dividends (in period): {self._money(div)}")
        self._total_value.setText(self._signed(total))
        self._total_value.setStyleSheet(
            f"color: {self._colour(total) or '#0f172a'}; "
            "font-size: 22px; font-weight: bold;"
        )
        self._roi_value.setText(
            f"Return on cost: {self._roi(float(total), float(cost_deployed))}"
        )

        notes: list[str] = []
        if unpriced:
            notes.append(
                f"{unpriced} held position(s) unpriced — excluded from market "
                "value & unrealized."
            )
        if self._convert_missing:
            notes.append("Some amounts lacked an FX rate and were not converted.")
        elif self._convert_fallback:
            notes.append("Some FX conversions used a nearest-prior rate.")
        self._note_value.setText("\n".join(notes))

    def _show_empty(self, message: str) -> None:
        self._chart.show_empty(message)
        self._table.setRowCount(0)
        self._period_value.setText(message)
        for lab in (self._cost_value, self._market_value, self._unrealized_value,
                    self._realized_value, self._dividends_value, self._filters_value,
                    self._roi_value, self._note_value):
            lab.setText("")
        self._total_value.setText("—")

    # ── filter dialog ──

    def _on_open_filter(self) -> None:
        dialog = InvestmentReturnsFilterDialog(
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

    def _on_save(self) -> None:
        if self._report_id is None:
            self._on_save_as()
            return
        try:
            row = self._repo.update_report(
                self._report_id,
                filters_json=self._current_filters.to_json(),
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
            title="Save As…" if self._report_id is not None else "Save report",
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
                type_key=TYPE_INVESTMENT_RETURNS,
                folder_id=choice.folder_id,
                filters_json=self._current_filters.to_json(),
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
            self._name_label.setText("Untitled Investment Returns")
            self._name_label.setStyleSheet(
                "color: #64748b; font-style: italic; "
                "font-weight: bold; padding: 4px 8px;"
            )
            self.setWindowTitle("Investment Returns — Untitled")
            return
        prefix = ""
        if self._loaded_folder_id is not None:
            for fdr in self._repo.list_report_folders():
                if fdr.id == self._loaded_folder_id:
                    prefix = f"{fdr.name} / "
                    break
        dirty_mark = "*" if self._dirty else ""
        self._name_label.setText(f"{prefix}{self._loaded_name}{dirty_mark}")
        self._name_label.setStyleSheet(
            "color: #334155; font-weight: bold; padding: 4px 8px;"
        )
        self.setWindowTitle(
            f"Investment Returns — {prefix}{self._loaded_name}{dirty_mark}"
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
