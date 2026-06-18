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

from mfl_desktop.db.repository import Repository, ReportRow
from mfl_desktop.holdings import ReturnPoint, compute_returns, xirr
from mfl_desktop.reports.filters import (
    InvestmentReturnsFilters, TYPE_INVESTMENT_RETURNS,
)
from mfl_desktop.ui.investment_returns_filter_dialog import (
    InvestmentReturnsFilterDialog,
)
from mfl_desktop.ui.returns_chart import ReturnsChart
from mfl_desktop.ui.save_report_as_dialog import SaveReportAsDialog
from mfl_desktop.ui import tokens
from mfl_desktop.ui.report_save import resolve_save_as
from mfl_desktop import periods
from dataclasses import replace

_CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥"}

# Period labels are the shared registry (ADR-082, single source of truth).

_GAIN = "#16a34a"
_LOSS = "#dc2626"

_TABLE_HEADERS = (
    "Symbol", "Security", "Cost", "Market value",
    "Unrealized", "Realized", "Dividends", "Total return", "Return %", "IRR / yr",
)


class _SortItem(QTableWidgetItem):
    """Table cell that sorts on a value stashed in ``Qt.UserRole`` rather than
    its display text — so a "$1,234.56" or "+12.3%" or "—" cell orders
    numerically. Falls back to case-insensitive text when no sort value is set
    (the Symbol / Security columns)."""

    def __lt__(self, other: "QTableWidgetItem") -> bool:
        a = self.data(Qt.UserRole)
        b = other.data(Qt.UserRole)
        if a is not None and b is not None:
            try:
                return float(a) < float(b)
            except (TypeError, ValueError):
                pass
        return self.text().casefold() < other.text().casefold()


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

        # Best/worst-performer panel state, rebuilt per refresh (the panel has
        # its OWN timeframe, independent of the report period).
        self._perf_candidates: list[tuple[int, str, str]] = []  # (sid, symbol, name)
        self._perf_series: dict[int, list[tuple[str, float]]] = {}  # sid → asc (date, price)

        # ── top bar ──
        self._name_label = QLabel()
        tokens.themed(self._name_label, "color: {heading}; font-weight: bold; padding: 4px 8px;")
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
        tokens.themed(top_rule, "color: {border};")

        # ── body: (chart over table) | summary ──
        self._chart = ReturnsChart()

        self._table = QTableWidget(0, len(_TABLE_HEADERS))
        self._table.setHorizontalHeaderLabels(_TABLE_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setAlternatingRowColors(True)
        # Click-to-sort. Numeric columns sort on a stored value (Qt.UserRole)
        # via _SortItem, not on the formatted "$1,234.56" / "—" text. The
        # indicator starts cleared so a fresh load keeps the report's default
        # order (held first, by total return); once the user clicks a header
        # the choice sticks across filter changes.
        self._table.setSortingEnabled(True)
        hh = self._table.horizontalHeader()
        hh.setSortIndicatorShown(True)
        hh.setSortIndicator(-1, Qt.AscendingOrder)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in range(2, len(_TABLE_HEADERS)):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        self._left_splitter = QSplitter(Qt.Vertical)
        self._left_splitter.addWidget(self._chart)
        self._left_splitter.addWidget(self._table)
        self._left_splitter.setStretchFactor(0, 1)
        self._left_splitter.setStretchFactor(1, 0)

        self._summary_panel = self._build_summary_panel()

        self._body_splitter = QSplitter(Qt.Horizontal)
        self._body_splitter.addWidget(self._left_splitter)
        self._body_splitter.addWidget(self._summary_panel)
        self._body_splitter.setStretchFactor(0, 1)
        self._body_splitter.setStretchFactor(1, 0)

        _f = self._current_filters
        self._left_splitter.setSizes(list(_f.chart_split) if _f.chart_split else [460, 280])
        self._body_splitter.setSizes(list(_f.body_split) if _f.body_split else [940, 300])
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
        tokens.themed(panel, "QFrame { background: {canvas}; border-left: 1px solid {border}; }QLabel { background: transparent; }")
        panel.setMinimumWidth(260)

        self._period_value = QLabel()
        self._period_value.setWordWrap(True)
        tokens.themed(self._period_value, "color: {text};")
        self._filters_value = QLabel()
        self._filters_value.setWordWrap(True)
        tokens.themed(self._filters_value, "color: {muted_strong};")

        self._cost_value = QLabel()
        self._market_value = QLabel()
        self._unrealized_value = QLabel()
        self._realized_value = QLabel()
        self._dividends_value = QLabel()
        for lab in (self._cost_value, self._market_value, self._unrealized_value,
                    self._realized_value, self._dividends_value):
            tokens.themed(lab, "color: {text};")

        self._total_value = QLabel()
        tokens.themed(self._total_value, "color: {text}; font-size: 22px; font-weight: bold;")
        self._roi_value = QLabel()
        tokens.themed(self._roi_value, "color: {muted_strong};")
        self._irr_value = QLabel()
        tokens.themed(self._irr_value, "color: {muted_strong};")
        self._irr_caption = QLabel(
            "Money-weighted, annualized — accounts for the timing & size of "
            "buys, sells and distributions."
        )
        self._irr_caption.setWordWrap(True)
        tokens.themed(self._irr_caption, "color: {subtle}; font-size: 10px;")
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
        layout.addWidget(self._irr_value)
        layout.addWidget(self._irr_caption)
        layout.addSpacing(6)
        layout.addWidget(self._note_value)
        layout.addSpacing(10)

        # ── Best / worst performers (own timeframe) ──
        layout.addWidget(self._mini_section_title("Performers"))
        self._perf_combo = QComboBox()
        for label, key in (
            ("1 month", "1m"), ("3 months", "3m"), ("6 months", "6m"),
            ("Year to date", "ytd"), ("1 year", "1y"),
        ):
            self._perf_combo.addItem(label, key)
        self._perf_combo.setCurrentIndex(4)   # 1 year
        self._perf_combo.currentIndexChanged.connect(
            lambda _i: self._update_performance()
        )
        layout.addWidget(self._perf_combo)
        perf_note = QLabel("Price change over the period (held, priced holdings).")
        perf_note.setWordWrap(True)
        tokens.themed(perf_note, "color: {subtle}; font-size: 10px;")
        layout.addWidget(perf_note)
        self._perf_container = QWidget()
        self._perf_layout = QVBoxLayout(self._perf_container)
        self._perf_layout.setContentsMargins(0, 4, 0, 0)
        self._perf_layout.setSpacing(3)
        layout.addWidget(self._perf_container)

        layout.addStretch(1)
        return panel

    @staticmethod
    def _mini_section_title(text: str) -> QLabel:
        lab = QLabel(text.upper())
        tokens.themed(lab, "color: {subtle}; font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        return lab

    # ── best / worst performers ──

    def _perf_start_date(self) -> str:
        """ISO start date for the performer panel's selected timeframe,
        measured back from today (YTD = Jan 1 of the current year)."""
        key = self._perf_combo.currentData()
        today = date.today()
        if key == "ytd":
            return date(today.year, 1, 1).isoformat()
        days = {"1m": 30, "3m": 91, "6m": 182, "1y": 365}.get(key, 365)
        return (today - timedelta(days=days)).isoformat()

    def _perf_row(self, symbol: str, name: str, perf: float) -> QWidget:
        """One performer line: ticker (or truncated name) left, coloured %% right."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        label = (symbol or "").strip() or (name[:18] + ("…" if len(name) > 18 else ""))
        left = QLabel(label)
        tokens.themed(left, "color: {text};")
        left.setToolTip(name)
        pct = QLabel(f"{perf:+.1f}%")
        pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        pct.setStyleSheet(
            f"color: {'#16a34a' if perf >= 0 else '#dc2626'}; font-weight: 600;"
        )
        h.addWidget(left, 1)
        h.addWidget(pct, 0)
        return w

    def _perf_subhead(self, text: str) -> QLabel:
        lab = QLabel(text)
        tokens.themed(lab, "color: {muted_strong}; font-weight: 600; margin-top: 4px;")
        return lab

    def _update_performance(self) -> None:
        """Rank the held, priced holdings by price change over the panel's
        timeframe and show the top / bottom few. Re-runs on a timeframe change
        without touching the DB (price series are cached at refresh time)."""
        # Clear the previous list.
        while self._perf_layout.count():
            item = self._perf_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        def note(text: str) -> None:
            lab = QLabel(text)
            lab.setWordWrap(True)
            tokens.themed(lab, "color: {subtle}; font-style: italic;")
            self._perf_layout.addWidget(lab)

        if not self._perf_candidates:
            note("No priced holdings to rank.")
            return

        start = self._perf_start_date()
        # Both ends must sit NEAR the window edges, else a sparsely-priced
        # holding's "nearest-prior" can reach back years and yield a nonsense
        # %. Require the start price within ~a month before the window start
        # and the latest price within ~a month of today — which also keeps the
        # ranking to genuinely price-tracked holdings.
        _TOL = 31
        start_floor = (date.fromisoformat(start) - timedelta(days=_TOL)).isoformat()
        recent_floor = (date.today() - timedelta(days=_TOL)).isoformat()
        ranked: list[tuple[float, str, str]] = []
        for sid, symbol, name in self._perf_candidates:
            series = self._perf_series.get(sid)
            if not series:
                continue
            end_date, end_price = series[-1]
            if not end_price or end_date < recent_floor:
                continue                        # no current price → can't rank
            start_price = start_date = None
            for d_iso, price in series:         # nearest price on/before start
                if d_iso <= start:
                    start_price, start_date = price, d_iso
                else:
                    break
            if not start_price or start_price <= 0 or start_date < start_floor:
                continue                        # no price near the window start
            ranked.append(((end_price - start_price) / start_price * 100.0, symbol, name))

        if not ranked:
            note("Not enough price history for this timeframe.")
            return

        ranked.sort(key=lambda x: x[0], reverse=True)
        best = ranked[:5]
        # Worst excludes any already shown as best (matters for a tiny portfolio).
        best_keys = {(s, n) for _p, s, n in best}
        worst = [r for r in reversed(ranked) if (r[1], r[2]) not in best_keys][:5]

        self._perf_layout.addWidget(self._perf_subhead("Best"))
        for perf, symbol, name in best:
            self._perf_layout.addWidget(self._perf_row(symbol, name, perf))
        if worst:
            self._perf_layout.addWidget(self._perf_subhead("Worst"))
            for perf, symbol, name in worst:   # most negative first
                self._perf_layout.addWidget(self._perf_row(symbol, name, perf))

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

    @staticmethod
    def _irr_text(irr: Optional[float]) -> str:
        """Annualized money-weighted return as a signed percent, ``—`` when
        undefined (no sign change / too few flows). Very short windows annualize
        to extreme rates; cap the display at ±999.9%/yr so the layout holds."""
        if irr is None:
            return "—"
        p = irr * 100
        if abs(p) >= 1000:
            return (">+999.9%" if p > 0 else "<-999.9%") + " / yr"
        sign = "+" if p >= 0 else "-"
        return f"{sign}{abs(p):.1f}% / yr"

    @staticmethod
    def _irr_cell(irr: Optional[float], priced: bool = True) -> str:
        """Compact per-security IRR for the table (no '/ yr' suffix — the header
        carries it). ``—`` when undefined; a trailing ``*`` when the figure used
        a cost fallback for an unpriced bookend/transfer (approximate)."""
        if irr is None:
            return "—"
        p = irr * 100
        mark = "" if priced else "*"
        if abs(p) >= 1000:
            return (">+999%" if p > 0 else "<-999%") + mark
        sign = "+" if p >= 0 else "-"
        return f"{sign}{abs(p):.1f}%{mark}"

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
        if key in ("ytd", "1y", "3y", "5y"):
            start, end = periods.period_bounds(key, today)
            return start, end
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

        # Group the selected accounts' full-history txns BY CURRENCY, then run
        # one FIFO replay per currency group. Pooling same-currency accounts is
        # what lets an in-kind share transfer between two accounts net out
        # (ShrsOut in one, ShrsIn in the other) — the holdings engine carries
        # the cost basis across the matched legs only when both are in the same
        # replay (ADR-053). Grouping by currency (not per-account) keeps the
        # mixed-currency conversion correct since a rate depends only on the
        # currency. Transfers across two *different* currencies remain the
        # round-4 transfer-linking concern.
        by_ccy: dict[str, dict] = {}   # currency → {"txns": [...], "sec_ids": set}
        earliest: Optional[date] = None
        for acct in accounts:
            txns = self._repo.list_transactions_for_account(acct.id)
            dated = [t.posted_date for t in txns if t.posted_date]
            if dated:
                first = date.fromisoformat(min(dated))
                earliest = first if earliest is None else min(earliest, first)
            g = by_ccy.setdefault(acct.currency, {"txns": [], "sec_ids": set()})
            g["txns"].extend(txns)
            g["sec_ids"].update(
                t.security_id for t in txns if t.security_id is not None
            )

        d_from, d_to = self._resolve_bounds(earliest)
        samples = _month_end_samples(d_from, d_to)
        samples_iso = sorted({d.isoformat() for d in samples})
        window_start = d_from.isoformat()
        end_iso = d_to.isoformat()

        # Display currency: native if uniform, else first account's currency.
        currencies = set(by_ccy)
        self._display_ccy = (
            next(iter(currencies)) if len(currencies) == 1
            else accounts[0].currency
        )

        results = []   # (currency, ReturnsResult)
        all_series: dict[int, list[tuple[str, float]]] = {}
        for ccy, g in by_ccy.items():
            pser = {
                sid: [(p.price_date, p.price) for p in self._repo.price_series(sid)]
                for sid in g["sec_ids"]
            }
            all_series.update(pser)            # reused by the performer panel
            results.append(
                (ccy, compute_returns(g["txns"], samples, pser, window_start, security_ids))
            )

        # Aggregate points by sample index (all results share samples_iso).
        n = len(samples_iso)
        agg_points: list[ReturnPoint] = []
        any_fallback = False
        for i in range(n):
            date_i = samples_iso[i]
            cost = mv = realized = div = Decimal("0")
            fully = True
            for ccy, res in results:
                if i >= len(res.points):
                    continue
                p = res.points[i]
                cost += self._conv(p.cost_basis, ccy, date_i)
                mv += self._conv(p.market_value, ccy, date_i)
                realized += self._conv(p.realized_cum, ccy, date_i)
                div += self._conv(p.dividends_cum, ccy, date_i)
                fully = fully and p.fully_priced
            any_fallback = any_fallback or not fully
            agg_points.append(ReturnPoint(
                date=date_i, cost_basis=cost, market_value=mv,
                unrealized=mv - cost, realized_cum=realized,
                dividends_cum=div, fully_priced=fully,
            ))

        # Aggregate per-security at end-of-window (convert at end date).
        merged: dict[int, dict] = {}
        for ccy, res in results:
            for s in res.by_security:
                m = merged.setdefault(s.security_id, {
                    "sid": s.security_id,
                    "symbol": s.symbol, "name": s.name, "shares": 0.0,
                    "cost": Decimal("0"), "cost_sold": Decimal("0"),
                    "mv": Decimal("0"),
                    "unreal": Decimal("0"), "realized": Decimal("0"),
                    "div": Decimal("0"), "priced": False,
                    # Per-security IRR inputs (converted to display ccy below).
                    "irr_flows": [], "irr_open": Decimal("0"),
                    "irr_term": Decimal("0"), "irr_priced": True,
                })
                m["shares"] += s.shares
                m["cost"] += self._conv(s.cost_basis, ccy, end_iso)
                m["cost_sold"] += self._conv(s.cost_basis_sold, ccy, end_iso)
                m["realized"] += self._conv(s.realized_window, ccy, end_iso)
                m["div"] += self._conv(s.dividends_window, ccy, end_iso)
                if s.market_value is not None:
                    m["mv"] += self._conv(s.market_value, ccy, end_iso)
                    m["priced"] = True
                if s.unrealized is not None:
                    m["unreal"] += self._conv(s.unrealized, ccy, end_iso)
                # Convert this security's flows at each flow's own date, and its
                # opening/terminal at the window edges, then accumulate.
                for d_iso, amt in s.cash_flows:
                    m["irr_flows"].append((d_iso, float(self._conv(amt, ccy, d_iso))))
                m["irr_open"] += self._conv(s.opening_market_value, ccy, window_start)
                m["irr_term"] += self._conv(s.terminal_market_value, ccy, end_iso)
                m["irr_priced"] = m["irr_priced"] and s.irr_fully_priced

        rows = list(merged.values())
        for m in rows:
            unreal = m["unreal"] if m["priced"] else Decimal("0")
            m["total"] = unreal + m["realized"] + m["div"]
            # Cost deployed = cost of shares still held + cost of shares sold in
            # the window — the capital that produced this row's return (ADR-046
            # amendment), so a fully-sold position shows its real cost, not £0.
            m["cost_total"] = m["cost"] + m["cost_sold"]
            # Per-security money-weighted return (ADR-046 amendment 2): bracket
            # the security's flows with its opening/terminal market value.
            sec_flows: list[tuple[str, float]] = []
            if m["irr_open"] != 0:
                sec_flows.append((window_start, -float(m["irr_open"])))
            sec_flows.extend(m["irr_flows"])
            if m["irr_term"] != 0:
                sec_flows.append((end_iso, float(m["irr_term"])))
            m["irr"] = xirr(sec_flows)
        rows.sort(key=lambda m: (
            0 if m["shares"] > 1e-9 else 1, -float(m["total"]), m["name"].lower(),
        ))

        # Performer panel inputs: held, priced holdings + their cached price
        # series (the panel ranks by price change over its own timeframe).
        self._perf_series = all_series
        self._perf_candidates = [
            (m["sid"], m["symbol"], m["name"])
            for m in rows if m["shares"] > 1e-9 and m["priced"]
        ]

        # Portfolio totals from merged rows (consistent with the table).
        tot_cost = sum((m["cost"] for m in rows if m["shares"] > 1e-9), Decimal("0"))
        tot_cost_deployed = sum((m["cost_total"] for m in rows), Decimal("0"))
        tot_mv = sum((m["mv"] for m in rows if m["priced"]), Decimal("0"))
        tot_unreal = sum((m["unreal"] for m in rows if m["priced"]), Decimal("0"))
        tot_realized = sum((m["realized"] for m in rows), Decimal("0"))
        tot_div = sum((m["div"] for m in rows), Decimal("0"))
        tot_total = tot_unreal + tot_realized + tot_div
        unpriced = sum(1 for m in rows if m["shares"] > 1e-9 and not m["priced"])

        # ── money-weighted return (IRR / XIRR), ADR-046 companion ──
        # Pool each currency group's native dated flows (converted to the display
        # currency at the flow's own date), then bracket with the opening market
        # value (a contribution at the window start) and the terminal market
        # value (a return at the window end). A single sign change → a unique
        # annualized rate; xirr returns None when the return is undefined.
        irr_flows: list[tuple[str, float]] = []
        opening_total = Decimal("0")
        terminal_total = Decimal("0")
        irr_priced = True
        for ccy, res in results:
            for d_iso, amt in res.cash_flows:
                irr_flows.append((d_iso, float(self._conv(amt, ccy, d_iso))))
            opening_total += self._conv(res.opening_market_value, ccy, window_start)
            terminal_total += self._conv(res.terminal_market_value, ccy, end_iso)
            irr_priced = irr_priced and res.irr_fully_priced
        flows: list[tuple[str, float]] = []
        if opening_total != 0:
            flows.append((window_start, -float(opening_total)))
        flows.extend(irr_flows)
        if terminal_total != 0:
            flows.append((end_iso, float(terminal_total)))
        irr = xirr(flows)

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
            irr=irr, irr_priced=irr_priced,
        )
        self._update_performance()

    def _populate_table(self, rows: list[dict]) -> None:
        # Disable sorting while filling, or each setItem would re-sort the
        # partly-built table and scramble row/column alignment. Re-enabled after
        # — Qt then applies the header's current sort indicator (none on a fresh
        # load → keeps the insertion order set by _refresh's rows.sort).
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))
        _LOW = float("-inf")   # unpriced cells ("—") sort to the bottom
        for r, m in enumerate(rows):
            priced = m["priced"]
            unreal = m["unreal"] if priced else None
            cost_total = float(m["cost_total"])
            # (display text, alignment, colour, numeric sort key — None = sort by text)
            cells = [
                (m["symbol"], Qt.AlignLeft, None, None),
                (m["name"], Qt.AlignLeft, None, None),
                (self._money(m["cost_total"]), Qt.AlignRight, None, cost_total),
                (
                    self._money(m["mv"]) if priced else "—",
                    Qt.AlignRight, None,
                    float(m["mv"]) if priced else _LOW,
                ),
                (
                    self._signed(unreal) + self._pct(float(unreal), float(m["cost"]))
                    if unreal is not None else "—",
                    Qt.AlignRight,
                    self._colour(unreal),
                    float(unreal) if unreal is not None else _LOW,
                ),
                (self._signed(m["realized"]), Qt.AlignRight, self._colour(m["realized"]),
                 float(m["realized"])),
                (self._money(m["div"]), Qt.AlignRight, None, float(m["div"])),
                (self._signed(m["total"]), Qt.AlignRight, self._colour(m["total"]),
                 float(m["total"])),
                (
                    self._roi(float(m["total"]), cost_total),
                    Qt.AlignRight,
                    self._colour(m["total"]),
                    float(m["total"]) / cost_total if cost_total else _LOW,
                ),
                (
                    self._irr_cell(m["irr"], m["irr_priced"]),
                    Qt.AlignRight,
                    self._colour(m["irr"] * 100) if m["irr"] is not None else None,
                    m["irr"] if m["irr"] is not None else _LOW,
                ),
            ]
            for c, (text, align, colour, sortkey) in enumerate(cells):
                item = _SortItem(text)
                item.setTextAlignment(int(align | Qt.AlignVCenter))
                if colour is not None:
                    item.setForeground(QColor(colour))
                if sortkey is not None:
                    item.setData(Qt.UserRole, sortkey)
                self._table.setItem(r, c, item)
        self._table.setSortingEnabled(True)

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
        irr=None, irr_priced=True,
    ) -> None:
        key = self._current_filters.period_key
        period_label = periods.period_label(key)
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
        self._unrealized_value.setStyleSheet(
            f"color: {self._colour(unreal) or tokens.c('text')};"
        )
        self._realized_value.setText(f"Realized (in period): {self._signed(realized)}")
        self._dividends_value.setText(f"Dividends (in period): {self._money(div)}")
        self._total_value.setText(self._signed(total))
        self._total_value.setStyleSheet(
            f"color: {self._colour(total) or tokens.c('text')}; "
            "font-size: 22px; font-weight: bold;"
        )
        self._roi_value.setText(
            f"Return on cost: {self._roi(float(total), float(cost_deployed))}"
        )
        self._irr_value.setText(f"Money-weighted (IRR): {self._irr_text(irr)}")
        irr_colour = self._colour(irr * 100) if irr is not None else None
        self._irr_value.setStyleSheet(f"color: {irr_colour or tokens.c('muted_strong')};")

        notes: list[str] = []
        if unpriced:
            notes.append(
                f"{unpriced} held position(s) unpriced — excluded from market "
                "value & unrealized."
            )
        if irr is not None and not irr_priced:
            notes.append(
                "IRR used a cost fallback for some opening/closing/transfer "
                "values that lacked a price — treat it as approximate."
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
                    self._roi_value, self._irr_value, self._note_value):
            lab.setText("")
        self._total_value.setText("—")
        self._perf_candidates = []
        self._perf_series = {}
        self._update_performance()

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
            title="Save As…" if self._report_id is not None else "Save report",
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        choice = dialog.values()
        if choice is None:
            return
        try:
            row = resolve_save_as(
                self, self._repo, self._report_id, TYPE_INVESTMENT_RETURNS,
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
            self._name_label.setText("Untitled Investment Returns")
            tokens.themed(self._name_label, "color: {muted}; font-style: italic; font-weight: bold; padding: 4px 8px;")
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
        tokens.themed(self._name_label, "color: {heading}; font-weight: bold; padding: 4px 8px;")
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
