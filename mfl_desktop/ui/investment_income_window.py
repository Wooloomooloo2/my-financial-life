"""Investment Income — per-security income & yield report (ADR-108).

A non-modal QMainWindow for income / FIRE investing: for one investment account
or the whole portfolio it shows, per security, the income received over the
window plus its yield on cost and on market, alongside the holdings columns
(price, shares, cost, market value, value gain, total gain). Above the table a
bar chart plots income per calendar month; a summary strip carries the FIRE
headline — total income, portfolio yields, and a projected forward annual
income (trailing-period run-rate).

Income = all distributions ``is_income`` classifies (cash dividends, bond
coupons / interest, cap-gain distributions) plus, when the filter's *Include
reinvested dividends* toggle is on, reinvested DRIPs valued at quantity × price
(ADR-089). The toggle, yields and chart all read the same pure aggregator
(:mod:`mfl_desktop.reports.investment_income`); the cost / market-value /
realized columns come from the holdings engine ``compute_returns`` (ADR-046),
so this view and the Investment Returns report agree.

Currency: when all selected accounts share a currency the report aggregates
natively; a mixed-currency selection converts each account's monetary figures
into the first account's currency via ``Repository.convert_amount`` (a note
flags it). Per-row Price and Currency stay native / informational.

Unlike the Investment Returns report this is a live analysis window — no saved
type, no migration (ADR-108).
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import Repository
from mfl_desktop.holdings import compute_returns
from mfl_desktop import periods
from mfl_desktop.reports.investment_income import (
    IncomeFilters, enumerate_months, income_by_month, income_by_security,
)
from mfl_desktop.ui import tokens
from mfl_desktop.ui.investment_income_chart import IncomeBarChart
from mfl_desktop.ui.investment_income_filter_dialog import (
    InvestmentIncomeFilterDialog,
)
from mfl_desktop.ui.transactions_list_window import (
    TransactionsListWindow, TxnListFilter,
)

_CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥"}
_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_SID_ROLE = Qt.UserRole + 1   # security id on the row's first cell (drill-down)
_EPS = 1e-9

# Income-first column order (ADR-108 r2): this is an *income* view, so the
# income + yields lead, right after the security identity; the holdings /
# price / total-return columns follow. The Income cell is bold so it reads as
# the focus and isn't confused with the (larger) Total gain column.
_TABLE_HEADERS = (
    "Symbol", "Security", "Ccy", "Income", "Yield/cost", "Yield/mkt",
    "Price", "Shares", "Cost", "Market value", "Value gain", "Weight",
    "Total gain",
)


def _sym(currency: Optional[str]) -> str:
    return _CURRENCY_SYMBOLS.get((currency or "").upper(), "")


def _month_end_samples(start: date, end: date) -> list[date]:
    """Sample dates spanning ``[start, end]``: the start, every month-end
    strictly between, and the end — drives the holdings replay's chart points."""
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


def _month_end_iso(month_key: str) -> str:
    """Last calendar day of a ``'YYYY-MM'`` key, as ISO — the date a month's
    income is converted on (FX nearest-prior fills a future month-end)."""
    y, m = int(month_key[:4]), int(month_key[5:7])
    return date(y, m, calendar.monthrange(y, m)[1]).isoformat()


class _SortItem(QTableWidgetItem):
    """Cell that sorts on a value stashed in ``Qt.UserRole`` rather than its
    formatted text, falling back to case-insensitive text when none is set."""

    def __lt__(self, other: "QTableWidgetItem") -> bool:
        a = self.data(Qt.UserRole)
        b = other.data(Qt.UserRole)
        if a is not None and b is not None:
            try:
                return float(a) < float(b)
            except (TypeError, ValueError):
                pass
        return self.text().casefold() < other.text().casefold()


class InvestmentIncomeWindow(QMainWindow):
    """Investment Income report window — live, not saved (ADR-108)."""

    def __init__(self, repo: Repository, *, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.resize(1200, 740)
        self.setWindowTitle("Investment Income")

        self._all_accounts = repo.list_investment_accounts()
        self._accounts_by_id = {a.id: a for a in self._all_accounts}
        self._filters = IncomeFilters.default()

        self._display_ccy = ""
        self._convert_missing = False
        self._convert_fallback = False
        self._last_d_from: Optional[date] = None
        self._last_d_to: Optional[date] = None

        # ── top bar ──
        self._title_label = QLabel("Investment Income")
        tokens.themed(self._title_label, "color: {heading}; font-weight: bold; padding: 4px 8px;")
        self._filter_button = QPushButton("Filter…")
        self._filter_button.clicked.connect(self._on_open_filter)

        top_bar = QWidget()
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 8, 10, 8)
        top_layout.setSpacing(8)
        top_layout.addWidget(self._title_label, stretch=1)
        top_layout.addWidget(self._filter_button)

        top_rule = QFrame()
        top_rule.setFrameShape(QFrame.HLine)
        top_rule.setFrameShadow(QFrame.Sunken)
        tokens.themed(top_rule, "color: {border};")

        # ── body: (chart over table) | summary ──
        self._chart = IncomeBarChart()

        self._table = QTableWidget(0, len(_TABLE_HEADERS))
        self._table.setHorizontalHeaderLabels(_TABLE_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.cellDoubleClicked.connect(self._on_security_row_activated)
        hh = self._table.horizontalHeader()
        hh.setSortIndicatorShown(True)
        hh.setSortIndicator(-1, Qt.AscendingOrder)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in range(2, len(_TABLE_HEADERS)):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        left_splitter = QSplitter(Qt.Vertical)
        left_splitter.addWidget(self._chart)
        left_splitter.addWidget(self._table)
        left_splitter.setStretchFactor(0, 0)
        left_splitter.setStretchFactor(1, 1)
        left_splitter.setSizes([240, 460])

        self._summary_panel = self._build_summary_panel()

        body_splitter = QSplitter(Qt.Horizontal)
        body_splitter.addWidget(left_splitter)
        body_splitter.addWidget(self._summary_panel)
        body_splitter.setStretchFactor(0, 1)
        body_splitter.setStretchFactor(1, 0)
        body_splitter.setSizes([900, 300])

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(top_bar)
        central_layout.addWidget(top_rule)
        central_layout.addWidget(body_splitter, stretch=1)
        self.setCentralWidget(central)

        self._refresh()

    # ── summary panel ──

    def _build_summary_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.NoFrame)
        tokens.themed(panel, "QFrame { background: {canvas}; border-left: 1px solid {border}; } QLabel { background: transparent; }")
        panel.setMinimumWidth(280)

        self._period_value = QLabel()
        self._period_value.setWordWrap(True)
        tokens.themed(self._period_value, "color: {text};")
        self._filters_value = QLabel()
        self._filters_value.setWordWrap(True)
        tokens.themed(self._filters_value, "color: {muted_strong};")

        self._income_value = QLabel()
        tokens.themed(self._income_value, "color: {text}; font-size: 22px; font-weight: bold;")
        self._yoc_value = QLabel()
        self._yom_value = QLabel()
        for lab in (self._yoc_value, self._yom_value):
            tokens.themed(lab, "color: {text};")
        self._projection_value = QLabel()
        tokens.themed(self._projection_value, "color: {text}; font-weight: bold;")
        self._projection_caption = QLabel(
            "Trailing-period run-rate — the income received, annualised over the "
            "selected window. Not a forecast of declared dividends."
        )
        self._projection_caption.setWordWrap(True)
        tokens.themed(self._projection_caption, "color: {subtle}; font-size: 10px;")
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
        layout.addWidget(self._mini_section_title("Income received"))
        layout.addWidget(self._income_value)
        layout.addWidget(self._yoc_value)
        layout.addWidget(self._yom_value)
        layout.addSpacing(6)
        layout.addWidget(self._mini_section_title("Projected annual income"))
        layout.addWidget(self._projection_value)
        layout.addWidget(self._projection_caption)
        layout.addSpacing(6)
        layout.addWidget(self._note_value)
        layout.addStretch(1)
        return panel

    @staticmethod
    def _mini_section_title(text: str) -> QLabel:
        lab = QLabel(text.upper())
        tokens.themed(lab, "color: {subtle}; font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        return lab

    # ── formatting ──

    def _money(self, amount, decimals: int = 2, ccy: Optional[str] = None) -> str:
        sym = _sym(ccy if ccy is not None else self._display_ccy)
        a = float(amount)
        sign = "-" if a < 0 else ""
        return f"{sign}{sym}{abs(a):,.{decimals}f}"

    def _signed(self, amount, decimals: int = 2) -> str:
        a = float(amount)
        sign = "+" if a >= 0 else "-"
        return f"{sign}{_sym(self._display_ccy)}{abs(a):,.{decimals}f}"

    @staticmethod
    def _pct(numer: float, denom: float) -> str:
        if not denom:
            return ""
        p = numer / denom * 100
        sign = "+" if p >= 0 else "-"
        return f" ({sign}{abs(p):.1f}%)"

    @staticmethod
    def _yield_text(income: float, denom: float) -> tuple[str, float]:
        """``(display, sort_key)`` for a yield = income / denom. ``—`` (sort to
        the bottom) when there's no denominator."""
        if denom <= 0:
            return "—", float("-inf")
        y = income / denom * 100
        return f"{y:.2f}%", y

    @staticmethod
    def _colour(amount) -> Optional[str]:
        if amount is None:
            return None
        a = float(amount)
        if a > 0:
            return tokens.c("positive")
        if a < 0:
            return tokens.c("negative")
        return None

    # ── period + conversion ──

    def _resolve_bounds(self, earliest: Optional[date]) -> tuple[date, date]:
        f = self._filters
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
        return (earliest or today), today

    def _conv(self, amount: Decimal, from_ccy: str, on_date: str) -> Decimal:
        if from_ccy == self._display_ccy:
            return amount
        converted, fallback = self._repo.convert_amount(
            amount, from_ccy=from_ccy, to_ccy=self._display_ccy, on_date=on_date,
        )
        if converted is None:
            self._convert_missing = True
            return amount
        if fallback:
            self._convert_fallback = True
        return converted

    @staticmethod
    def _dec(amount: float) -> Decimal:
        return Decimal(str(round(amount, 2)))

    # ── refresh ──

    def _refresh(self) -> None:
        self._convert_missing = False
        self._convert_fallback = False
        f = self._filters

        if not self._all_accounts:
            self._show_empty("No investment accounts yet.")
            return
        account_ids = list(f.account_ids) or [a.id for a in self._all_accounts]
        accounts = [self._accounts_by_id[i] for i in account_ids
                    if i in self._accounts_by_id]
        if not accounts:
            self._show_empty("Select at least one account.")
            return

        # Group the selected accounts' full-history txns by currency, then run
        # one holdings replay + one income pass per currency group (mirrors the
        # Investment Returns window so same-currency in-kind transfers net out).
        by_ccy: dict[str, dict] = {}
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
        self._last_d_from, self._last_d_to = d_from, d_to
        window_start = d_from.isoformat()
        end_iso = d_to.isoformat()
        samples = _month_end_samples(d_from, d_to)

        currencies = set(by_ccy)
        self._display_ccy = (
            next(iter(currencies)) if len(currencies) == 1
            else accounts[0].currency
        )

        multipliers = self._repo.security_multipliers()
        merged: dict[int, dict] = {}
        month_display: dict[str, Decimal] = {}

        def row_for(sid: int, ccy: str) -> dict:
            return merged.setdefault(sid, {
                "sid": sid, "symbol": "", "name": "", "ccy": ccy,
                "shares": 0.0, "cost": Decimal("0"), "mv": Decimal("0"),
                "unreal": Decimal("0"), "realized": Decimal("0"),
                "income": Decimal("0"), "price": None, "priced": False,
            })

        for ccy, g in by_ccy.items():
            pser = {
                sid: [(p.price_date, p.price) for p in self._repo.price_series(sid)]
                for sid in g["sec_ids"]
            }
            res = compute_returns(
                g["txns"], samples, pser, window_start, None, multipliers,
            )
            inc_sec = income_by_security(
                g["txns"], window_start, end_iso, multipliers, f.include_reinvested,
            )
            inc_month = income_by_month(
                g["txns"], window_start, end_iso, multipliers, f.include_reinvested,
            )

            for s in res.by_security:
                m = row_for(s.security_id, ccy)
                m["symbol"] = s.symbol
                m["name"] = s.name
                m["shares"] += s.shares
                m["cost"] += self._conv(s.cost_basis, ccy, end_iso)
                m["realized"] += self._conv(s.realized_window, ccy, end_iso)
                if s.market_value is not None:
                    m["mv"] += self._conv(s.market_value, ccy, end_iso)
                    m["priced"] = True
                if s.unrealized is not None:
                    m["unreal"] += self._conv(s.unrealized, ccy, end_iso)
                ser = pser.get(s.security_id)
                if ser:
                    m["price"] = ser[-1][1]      # latest native price
            for sid, amt in inc_sec.items():
                row_for(sid, ccy)["income"] += self._conv(self._dec(amt), ccy, end_iso)
            for mkey, amt in inc_month.items():
                conv = self._conv(self._dec(amt), ccy, _month_end_iso(mkey))
                month_display[mkey] = month_display.get(mkey, Decimal("0")) + conv

        rows = [m for m in merged.values()
                if m["shares"] > _EPS or m["income"] != 0]
        rows.sort(key=lambda m: (-float(m["income"]), m["name"].lower()))

        tot_income = sum((m["income"] for m in rows), Decimal("0"))
        tot_cost = sum((m["cost"] for m in rows if m["shares"] > _EPS), Decimal("0"))
        tot_mv = sum((m["mv"] for m in rows if m["priced"]), Decimal("0"))

        self._populate_table(rows, tot_mv)
        self._render_chart(d_from, d_to, month_display)
        self._update_summary(
            d_from=d_from, d_to=d_to, accounts=accounts,
            tot_income=tot_income, tot_cost=tot_cost, tot_mv=tot_mv,
        )

    def _populate_table(self, rows: list[dict], tot_mv: Decimal) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))
        _LOW = float("-inf")
        tot_mv_f = float(tot_mv)
        for r, m in enumerate(rows):
            priced = m["priced"]
            shares = m["shares"]
            unreal = m["unreal"] if priced else None
            mv = m["mv"] if priced else None
            income = float(m["income"])
            cost = float(m["cost"])
            mv_f = float(m["mv"]) if priced else 0.0
            total_gain = (float(unreal) if unreal is not None else 0.0) \
                + float(m["realized"]) + income
            weight = (mv_f / tot_mv_f * 100) if (priced and tot_mv_f) else None
            yoc_text, yoc_key = self._yield_text(income, cost)
            yom_text, yom_key = self._yield_text(income, mv_f) if priced else ("—", _LOW)
            price = m["price"]

            # (text, alignment, colour, numeric sort key, bold)
            cells = [
                (m["symbol"], Qt.AlignLeft, None, None, False),
                (m["name"], Qt.AlignLeft, None, None, False),
                (m["ccy"], Qt.AlignLeft, None, None, False),
                (self._money(m["income"]), Qt.AlignRight, None, income, True),
                (yoc_text, Qt.AlignRight, None, yoc_key, False),
                (yom_text, Qt.AlignRight, None, yom_key, False),
                (
                    self._money(price, 2, m["ccy"]) if price is not None else "—",
                    Qt.AlignRight, None, float(price) if price is not None else _LOW,
                    False,
                ),
                (f"{shares:,.4f}", Qt.AlignRight, None, shares, False),
                (self._money(m["cost"]), Qt.AlignRight, None, cost, False),
                (
                    self._money(mv) if priced else "—",
                    Qt.AlignRight, None, mv_f if priced else _LOW, False,
                ),
                (
                    self._signed(unreal) + self._pct(float(unreal), cost)
                    if unreal is not None else "—",
                    Qt.AlignRight, self._colour(unreal),
                    float(unreal) if unreal is not None else _LOW, False,
                ),
                (
                    f"{weight:.1f}%" if weight is not None else "—",
                    Qt.AlignRight, None, weight if weight is not None else _LOW,
                    False,
                ),
                (
                    self._signed(total_gain), Qt.AlignRight,
                    self._colour(total_gain), total_gain, False,
                ),
            ]
            for c, (text, align, colour, sortkey, bold) in enumerate(cells):
                item = _SortItem(text)
                item.setTextAlignment(int(align | Qt.AlignVCenter))
                if colour is not None:
                    item.setForeground(QColor(colour))
                if sortkey is not None:
                    item.setData(Qt.UserRole, sortkey)
                if bold:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if c == 0:
                    item.setData(_SID_ROLE, m["sid"])
                self._table.setItem(r, c, item)
        self._table.setSortingEnabled(True)

    def _render_chart(
        self, d_from: date, d_to: date, month_display: dict[str, Decimal],
    ) -> None:
        keys = enumerate_months(d_from.isoformat(), d_to.isoformat())
        if not keys or all(float(month_display.get(k, 0)) <= 0 for k in keys):
            self._chart.show_empty("No income in this period.")
            return
        multi_year = keys[0][:4] != keys[-1][:4]
        bars: list[tuple[str, float]] = []
        for k in keys:
            mon = _MONTH_ABBR[int(k[5:7]) - 1]
            label = f"{mon} ’{k[2:4]}" if (multi_year and k[5:7] == "01") else mon
            bars.append((label, float(month_display.get(k, Decimal("0")))))
        self._chart.render(bars, _sym(self._display_ccy))

    def _update_summary(
        self, *, d_from: date, d_to: date, accounts, tot_income, tot_cost, tot_mv,
    ) -> None:
        key = self._filters.period_key
        self._period_value.setText(
            f"{periods.period_label(key)}\n{d_from.isoformat()} → {d_to.isoformat()}"
        )
        if not self._filters.account_ids:
            acct_line = f"Accounts: all ({len(self._all_accounts)} — whole portfolio)"
        else:
            acct_line = f"Accounts: {len(accounts)} of {len(self._all_accounts)}"
        reinv_line = (
            "Reinvested dividends: included"
            if self._filters.include_reinvested else "Reinvested dividends: excluded"
        )
        ccy_line = f"Currency: {self._display_ccy}" if self._display_ccy else ""
        self._filters_value.setText("\n".join(
            x for x in (acct_line, reinv_line, ccy_line) if x
        ))

        self._income_value.setText(self._money(tot_income))
        yoc, _ = self._yield_text(float(tot_income), float(tot_cost))
        yom, _ = self._yield_text(float(tot_income), float(tot_mv))
        self._yoc_value.setText(f"Yield on cost: {yoc}")
        self._yom_value.setText(f"Yield on market: {yom}")

        # Projected forward annual income = the window's income annualised by its
        # fraction of a year (a trailing run-rate, not a forecast). For the 1y
        # default this is ~= the income received.
        days = max((d_to - d_from).days, 1)
        projection = float(tot_income) * 365.0 / days
        self._projection_value.setText(self._money(projection))

        notes: list[str] = []
        if key not in ("1y", "12m"):
            notes.append(
                "Yield columns are for the selected period; see Projected annual "
                "income for an annualised figure."
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
        for lab in (self._filters_value, self._yoc_value, self._yom_value,
                    self._projection_value, self._projection_caption,
                    self._note_value):
            lab.setText("")
        self._income_value.setText("—")

    # ── drill-down + filter ──

    def _on_security_row_activated(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)
        if item is None:
            return
        sid = item.data(_SID_ROLE)
        if sid is None or self._last_d_from is None or self._last_d_to is None:
            return
        name_item = self._table.item(row, 1)
        label = item.text() or (name_item.text() if name_item is not None else "")
        acc_ids = list(self._filters.account_ids)
        if len(acc_ids) == 1:
            account_id: Optional[int] = acc_ids[0]
            acct = self._accounts_by_id.get(account_id)
            account_name = acct.name if acct is not None else ""
        else:
            account_id, account_name = None, ""
        flt = TxnListFilter.for_security(
            account_id=account_id, account_name=account_name,
            security_id=int(sid), security_label=label,
            period_key="custom",
            custom_start=self._last_d_from, custom_end=self._last_d_to,
        )
        win = TransactionsListWindow(self._repo, flt, parent=self)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.show()

    def _on_open_filter(self) -> None:
        dialog = InvestmentIncomeFilterDialog(
            self._repo, current=self._filters, accounts=self._all_accounts,
            parent=self,
        )
        accepted = dialog.exec() == QDialog.Accepted
        # ADR-105: keep this report in front after the modal filter closes.
        self.raise_()
        self.activateWindow()
        if not accepted:
            return
        new_filters = dialog.values()
        if new_filters is None or new_filters == self._filters:
            return
        self._filters = new_filters
        self._refresh()
