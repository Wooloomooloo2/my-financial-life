"""Per-account summary screen (ADR-033).

A non-modal ``QMainWindow`` per account, opened from the sidebar
context menu, double-click, or the Account menu. Single-instance per
account — the owning RegisterWindow keeps a ``{account_id: window}``
registry; opening an account that already has a window raises and
focuses it.

Layout (Banktivity-inspired, two-row split):

- Top row, ``QSplitter(Horizontal)``:
    - Left: ACCOUNT BALANCE combo chart + period selector + Report panel.
    - Right: Summary / Additional Info / Upcoming / Reconcile placeholder.
- Bottom row, ``QSplitter(Horizontal)``:
    - Left: Top Payees panel (strict outflow, period-scoped).
    - Right: Top Categories panel (strict outflow, period-scoped).

The two rows sit in a ``QSplitter(Vertical)`` so the owner can give more
room to either the chart strip or the breakdowns.

Refresh on ``WindowActivate`` (same idiom as :class:`BudgetWindow`)
so flipping back from the register after an edit reflects new data.
"""
from __future__ import annotations

from mfl_desktop.ui import tokens
import mfl_desktop.ui.chart_helpers as _ch

from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.holdings import HoldingsView, compute_holdings_view
from mfl_desktop.ui.check_list_panel import CheckListPanel
from mfl_desktop.ui.value_chart import ValueBar, ValueChart
from mfl_desktop.ui.value_history_chart import ValueHistoryChart
from mfl_desktop.ui.treemap_chart import TreemapChart, TreemapTile
from mfl_desktop.holdings import compute_value_history
from mfl_desktop.account_summary import (
    PERIOD_KEYS,
    PERIOD_LABELS,
    BalanceFlowSeries,
    PeriodSummary,
    StatusBreakdown,
    TopNRow,
    UpcomingScheduled,
    compute_balance_flow_series,
    compute_period_summary,
    compute_status_breakdown,
    count_scheduled_for_account,
    period_bounds,
    period_display_label,
    pick_granularity,
    top_categories,
    top_payees,
    upcoming_scheduled,
)
from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.ui.balance_flow_chart import BalanceFlowChart
from mfl_desktop.ui.custom_period_dialog import CustomPeriodDialog
from mfl_desktop.ui.statements_window import StatementsWindow
from mfl_desktop.ui.transactions_list_window import (
    TransactionsListWindow,
    TxnListFilter,
)
from mfl_desktop.ui.ui_fonts import set_pt


# Tailwind v3 vocabulary — kept local because these are screen-level
# accents rather than chart series colours.
# Section card palette (ADR-034 §2): cards float on a slate-50 canvas
# with a soft slate-200 border so the screen reads as a grid of units.

_DEFAULT_PERIOD = "quarter"   # rolling 90 days — replaces the old "90d" key
_NON_CASH_FAMILIES = {"investment", "property", "vehicle"}

# The screen's money formatters are GBP-hardcoded (a known display-currency
# limitation); the holdings panel formats in the account's own currency so the
# USD figures read correctly. A per-report display-currency selector is a
# separate backlog item.
_CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥"}


def _sym(currency: str) -> str:
    return _CURRENCY_SYMBOLS.get((currency or "").upper(), "")


def _month_end_samples(first_iso: str, today: date) -> list[date]:
    """Month-end dates from the month of ``first_iso`` through ``today``
    (inclusive of today as the final point). Drives the value-over-time
    chart's x-axis (ADR-045)."""
    from datetime import timedelta
    try:
        d = date.fromisoformat(first_iso)
    except ValueError:
        return [today]
    y, m = d.year, d.month
    out: list[date] = []
    while True:
        nm_y, nm_m = (y + 1, 1) if m == 12 else (y, m + 1)
        eom = date(nm_y, nm_m, 1) - timedelta(days=1)
        if eom >= today:
            break
        out.append(eom)
        y, m = nm_y, nm_m
    out.append(today)   # always finish on today so the last point is current
    return out


def _fmt_shares(value: float) -> str:
    """Share quantity: up to 6dp, trailing zeros trimmed (180.0 → '180',
    0.069 → '0.069')."""
    s = f"{float(value):,.6f}".rstrip("0").rstrip(".")
    return s or "0"


def _fmt_ccy(amount: Decimal, currency: str, *, decimals: int = 2, signed: bool = False) -> str:
    sym = _sym(currency)
    sign = ""
    if amount < 0:
        sign = "-"
        amount = -amount
    elif signed:
        sign = "+"
    body = f"{sym}{amount:,.{decimals}f}" if sym else f"{amount:,.{decimals}f} {currency.upper()}"
    return f"{sign}{body}"


def _fmt_money(amount: Decimal) -> str:
    """Signed pence-precision GBP. Negative values render with a leading
    minus OUTSIDE the symbol ('-£40.00'), matching the rest of the app."""
    if amount < 0:
        return f"-£{(-amount):,.2f}"
    return f"£{amount:,.2f}"


def _fmt_money_no_decimals(amount: Decimal) -> str:
    """Big-number formatter for the period summary lines."""
    if amount < 0:
        return f"-£{(-amount):,.0f}"
    return f"£{amount:,.0f}"


class _TopNList(QWidget):
    """One bar-list row per :class:`TopNRow` — label / proportional bar /
    amount. Empty state shows a muted "Nothing in this period yet." line.

    Rows with a non-``None`` ``entity_id`` are clickable (ADR-034):
    hovering tints the row background slate-50 and switches the cursor
    to a pointing hand; a press fires :pyattr:`row_clicked`. Rows with
    ``entity_id is None`` (synthetic ``(No payee)`` / ``(Uncategorised)``
    buckets) are display-only — the drill-down doesn't have a "rows
    with NULL payee" filter wired in v1.
    """

    row_clicked = Signal(object)   # emits the TopNRow that was clicked

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: list[TopNRow] = []
        self._empty_message = "Nothing in this period yet."
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

        # Per-paint hitmap: list of (row_rect, row_index). Repopulated each
        # paintEvent so hit-testing stays in sync with the layout.
        self._hitmap: list[tuple[QRectF, int]] = []
        self._hover_index: Optional[int] = None

    def set_rows(self, rows: list[TopNRow]) -> None:
        self._rows = rows
        self._hover_index = None
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(_ch.chart_surface()))

        self._hitmap.clear()

        if not self._rows:
            painter.setPen(QPen(QColor(tokens.c("subtle"))))
            font = QFont(painter.font())
            set_pt(font, 10)
            painter.setFont(font)
            painter.drawText(
                self.rect(), Qt.AlignCenter, self._empty_message,
            )
            painter.end()
            return

        n = len(self._rows)
        row_h = max(22, min(34, int(self.height() / max(n, 1))))
        font = QFont(painter.font())
        set_pt(font, 10)
        painter.setFont(font)

        # Right-side amount column gets enough width for the largest value.
        amount_strs = [_fmt_money_no_decimals(r.amount) for r in self._rows]
        amount_w = max(
            (painter.fontMetrics().horizontalAdvance(s) for s in amount_strs),
            default=80,
        ) + 4

        # Left label column — cap at 40% of width to leave room for the bar.
        label_w = int(self.width() * 0.36)
        # Bar lives between the label column and the amount column.
        bar_x = label_w + 12
        bar_w_total = self.width() - bar_x - amount_w - 8
        if bar_w_total < 40:
            bar_w_total = max(1, self.width() - amount_w - 8)
            label_w = 0
            bar_x = 4

        for i, row in enumerate(self._rows):
            y = i * row_h
            row_rect = QRectF(0, y, self.width(), row_h)
            self._hitmap.append((row_rect, i))

            # Hover highlight on clickable rows only.
            if i == self._hover_index and row.entity_id is not None:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(QColor(tokens.c("surface_alt"))))
                painter.drawRect(row_rect)

            # Label (left).
            painter.setPen(QPen(QColor(tokens.c("text"))))
            label_rect = QRectF(8, y, label_w, row_h)
            painter.drawText(
                label_rect,
                Qt.AlignVCenter | Qt.AlignLeft,
                _elide(row.label, painter.fontMetrics(), label_rect.width() - 4),
            )

            # Bar track + fill (middle).
            bar_y = y + (row_h - 8) / 2
            track = QRectF(bar_x, bar_y, bar_w_total, 8)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(tokens.c("surface_alt"))))
            painter.drawRoundedRect(track, 4, 4)
            fill_w = max(0.0, bar_w_total * max(0.0, min(1.0, row.proportion)))
            if fill_w > 0:
                fill = QRectF(bar_x, bar_y, fill_w, 8)
                painter.setBrush(QBrush(QColor(tokens.c("accent_subtle"))))
                painter.drawRoundedRect(fill, 4, 4)

            # Amount (right).
            painter.setPen(QPen(QColor(tokens.c("text"))))
            amt_rect = QRectF(
                self.width() - amount_w - 4, y, amount_w, row_h,
            )
            painter.drawText(
                amt_rect,
                Qt.AlignVCenter | Qt.AlignRight,
                amount_strs[i],
            )

        painter.end()

    # ── hover / click ──

    def mouseMoveEvent(self, event) -> None:  # noqa: D401 — Qt override
        pos = event.position() if hasattr(event, "position") else event.posF()
        new_hover = None
        for rect, idx in self._hitmap:
            if rect.contains(pos):
                new_hover = idx
                break
        if new_hover != self._hover_index:
            self._hover_index = new_hover
            self.update()
        # Pointing-hand cursor over real (clickable) rows only.
        if new_hover is not None and self._rows[new_hover].entity_id is not None:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D401 — Qt override
        if self._hover_index is not None:
            self._hover_index = None
            self.update()
        self.unsetCursor()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: D401 — Qt override
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position() if hasattr(event, "position") else event.posF()
        for rect, idx in self._hitmap:
            if rect.contains(pos):
                row = self._rows[idx]
                if row.entity_id is not None:
                    self.row_clicked.emit(row)
                return
        super().mousePressEvent(event)


def _elide(text: str, fm, max_width: float) -> str:
    """Truncate text with an ellipsis if it overflows ``max_width``.
    Kept inline so the Top-N widget stays self-contained."""
    if fm.horizontalAdvance(text) <= max_width:
        return text
    ell = "…"
    while text and fm.horizontalAdvance(text + ell) > max_width:
        text = text[:-1]
    return text + ell


class AccountSummaryWindow(QMainWindow):
    """Per-account focus screen. Construct with ``(repo, account_id)``;
    the constructor pulls everything else from the repository."""

    def __init__(
        self,
        repo: Repository,
        account_id: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._account_id = account_id
        self._current_period: str = _DEFAULT_PERIOD
        # When _current_period == "custom", these carry the picked bounds;
        # otherwise they're either None or stale leftovers from a previous
        # custom selection (kept so re-opening the dialog defaults to the
        # last bounds the user chose).
        self._custom_start: Optional[date] = None
        self._custom_end: Optional[date] = None
        # Tracked so cancelling the Custom dialog can restore the
        # previously-checked preset button.
        self._previous_period: str = _DEFAULT_PERIOD
        self._period_buttons: dict[str, QPushButton] = {}

        # Pull the account up-front so we have its name + family for the
        # title + non-cash banner. If the account vanished mid-flight
        # (e.g. owner deleted it from another path), close immediately.
        account = self._repo.get_account_by_id(account_id)
        if account is None:
            self.close()
            return
        self._account: AccountSummary = account
        self.setWindowTitle(f"{account.name} · Summary")
        self.resize(1240, 760)

        # Single-instance per drill-down filter signature — ADR-034 §3
        # window policy: a repeat click on the same Top-N row raises the
        # existing window; clicking a different row spawns a new one.
        self._drilldown_wins: dict[tuple, TransactionsListWindow] = {}

        # An investment account gets a tabbed dashboard (ADR-045) — a value
        # chart + a roomy searchable Holdings tab — instead of the cash
        # account's single-page chart/info/top-N layout.
        self._is_investment = self._account.family == "investment"
        # A loan account (ADR-095) gets its amortization schedule + chart instead
        # of the cash account's chart / info / Top-N panels (a loan has no payees
        # or spending categories to rank).
        self._is_loan = self._account.family == "loan"
        # Value-chart selection state (investment only): which securities are
        # charted (None = all) and whether to collapse to one portfolio bar.
        self._chart_selected_ids: Optional[set[int]] = None
        self._chart_total_mode = False
        # Latest computed holdings — kept so the Holdings search box and the
        # chart-selection controls can re-render without recomputing.
        self._holdings_view: Optional[HoldingsView] = None

        if self._is_loan:
            content = self._build_loan_layout()
        else:
            info_panel = self._build_info_panel()
            content = (
                self._build_investment_tabs(info_panel)
                if self._is_investment
                else self._build_cash_layout(info_panel)
            )

        container = QWidget()
        container.setObjectName("summaryRoot")
        tokens.themed(container, "QWidget#summaryRoot { background-color: {canvas}; }")
        v = QVBoxLayout(container)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)
        v.addWidget(self._build_title())
        v.addWidget(content, stretch=1)
        self.setCentralWidget(container)

        self.reload()

    def _build_loan_layout(self) -> QWidget:
        """A loan account's amortization schedule + balance chart (ADR-095). No
        cash-flow chart, info panel, or Top-N — a loan has no payees or spending
        categories to rank."""
        from mfl_desktop.ui.loan_schedule_view import LoanScheduleWidget
        self._loan_view = LoanScheduleWidget(self._repo, self._account_id)
        self._loan_view.changed.connect(self._on_loan_changed)
        return self._loan_view

    def _on_loan_changed(self) -> None:
        """A recorded payment / edit moved the balance — keep the cached account
        (and so the title) current."""
        self._account = (
            self._repo.get_account_by_id(self._account_id) or self._account
        )

    def _build_cash_layout(self, info_panel: QWidget) -> QWidget:
        """The original single-page layout (ADR-033/034): chart | info over a
        Top-Payees | Top-Categories row. Cash accounts only."""
        chart_panel = self._build_chart_panel()
        top_payees_panel = self._build_top_n_panel(
            title="TOP PAYEES (this period)", widget_attr="_top_payees_widget",
        )
        top_categories_panel = self._build_top_n_panel(
            title="TOP CATEGORIES (this period)",
            widget_attr="_top_categories_widget",
        )
        self._top_payees_widget.row_clicked.connect(self._on_payee_clicked)
        self._top_categories_widget.row_clicked.connect(self._on_category_clicked)

        top_split = QSplitter(Qt.Horizontal)
        top_split.addWidget(chart_panel)
        top_split.addWidget(info_panel)
        top_split.setStretchFactor(0, 3)
        top_split.setStretchFactor(1, 2)
        top_split.setSizes([720, 480])
        top_split.setHandleWidth(12)
        top_split.setChildrenCollapsible(False)

        bottom_split = QSplitter(Qt.Horizontal)
        bottom_split.addWidget(top_payees_panel)
        bottom_split.addWidget(top_categories_panel)
        bottom_split.setStretchFactor(0, 1)
        bottom_split.setStretchFactor(1, 1)
        bottom_split.setSizes([600, 600])
        bottom_split.setHandleWidth(12)
        bottom_split.setChildrenCollapsible(False)

        outer = QSplitter(Qt.Vertical)
        outer.addWidget(top_split)
        outer.addWidget(bottom_split)
        outer.setStretchFactor(0, 3)
        outer.setStretchFactor(1, 2)
        outer.setSizes([460, 260])
        outer.setHandleWidth(12)
        outer.setChildrenCollapsible(False)
        return outer

    def _build_investment_tabs(self, info_panel: QWidget) -> QWidget:
        """Tabbed investment dashboard (ADR-045). Overview (portfolio value
        over time + info), Holdings (searchable wide table), Portfolio
        (allocation treemap by default, with a cost-vs-value bars view).
        Returns/Dividends land later."""
        tabs = QTabWidget()

        overview = QSplitter(Qt.Horizontal)
        overview.addWidget(self._build_overview_panel())
        overview.addWidget(info_panel)
        overview.setStretchFactor(0, 3)
        overview.setStretchFactor(1, 2)
        overview.setSizes([760, 440])
        overview.setHandleWidth(12)
        overview.setChildrenCollapsible(False)

        tabs.addTab(overview, "Overview")
        tabs.addTab(self._build_holdings_panel(), "Holdings")
        tabs.addTab(self._build_value_panel(), "Portfolio")
        return tabs

    def _build_overview_panel(self) -> QWidget:
        """The Overview tab's main card: portfolio value over time (ADR-045)."""
        panel = self._make_card("valueHistoryCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        header = QLabel("PORTFOLIO VALUE OVER TIME")
        tokens.themed(header, "color: {muted}; letter-spacing: 1px; font-size: 12px;")
        layout.addWidget(header)

        # The unpriced/valuations banner lives here on the Overview tab.
        self._non_cash_banner = QLabel("")
        tokens.themed(self._non_cash_banner, "color: {warning}; background-color: {accent_subtle}; padding: 6px 10px; border-radius: 4px; font-size: 12px;")
        self._non_cash_banner.setWordWrap(True)
        self._non_cash_banner.hide()
        layout.addWidget(self._non_cash_banner)

        self._value_history_chart = ValueHistoryChart()
        layout.addWidget(self._value_history_chart, stretch=1)
        return panel

    # ── builders ──

    def _build_title(self) -> QWidget:
        title = QLabel(self._account.name)
        f = title.font()
        f.setPointSize(f.pointSize() + 8)
        f.setBold(True)
        title.setFont(f)
        tokens.themed(title, "color: {text};")
        return title

    def _make_card(self, name: str) -> QFrame:
        """QFrame styled as a card (ADR-034 §2). The objectName scopes
        the rounded border + background so child widgets don't inherit
        it. Child QFrames (e.g. the inline separators) need to disable
        their own border to look right inside a card."""
        card = QFrame()
        card.setObjectName(name)
        tokens.themed(
            card,
            "QFrame#%s { background-color: {surface}; "
            "border: 1px solid {border}; border-radius: 10px; }" % name,
        )
        return card

    def _build_chart_panel(self) -> QWidget:
        panel = self._make_card("chartCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        header = QLabel("ACCOUNT BALANCE")
        tokens.themed(header, "color: {muted}; letter-spacing: 1px; font-size: 12px;")
        layout.addWidget(header)

        # Banner for non-cash families: investment / property / vehicle.
        # Hidden by default; toggled in reload() based on account.family.
        self._non_cash_banner = QLabel(
            "Balance reflects recorded transactions; valuations not yet wired."
        )
        tokens.themed(self._non_cash_banner, "color: {warning}; background-color: {accent_subtle}; padding: 6px 10px; border-radius: 4px; font-size: 12px;")
        self._non_cash_banner.setWordWrap(True)
        self._non_cash_banner.hide()
        layout.addWidget(self._non_cash_banner)

        self._chart = BalanceFlowChart()
        layout.addWidget(self._chart, stretch=1)

        layout.addWidget(self._build_period_selector())

        layout.addWidget(self._build_separator())
        layout.addLayout(self._build_report_panel())
        return panel

    def _build_period_selector(self) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 4, 0, 4)
        h.setSpacing(6)

        self._period_group = QButtonGroup(self)
        self._period_group.setExclusive(True)
        for key in PERIOD_KEYS:
            btn = QPushButton(PERIOD_LABELS[key])
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            tokens.themed(btn, "QPushButton { padding: 5px 12px; border: 1px solid {border_strong}; border-radius: 14px; background-color: {surface}; color: {heading}; font-size: 12px; }QPushButton:checked { background-color: {accent}; color: {surface}; border-color: {accent}; font-weight: bold; }QPushButton:hover:!checked { background-color: {surface_alt}; }")
            btn.clicked.connect(
                lambda _checked=False, k=key: self._on_period_selected(k)
            )
            h.addWidget(btn)
            self._period_buttons[key] = btn
            self._period_group.addButton(btn)
        h.addStretch(1)

        # Pre-select default period.
        self._period_buttons[self._current_period].setChecked(True)
        return row

    def _build_report_panel(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)

        self._report_header = QLabel("REPORT: ")
        tokens.themed(self._report_header, "color: {muted}; letter-spacing: 1px; font-size: 12px;")
        layout.addWidget(self._report_header)

        self._report_opening_lbl = QLabel("£0")
        self._report_inflows_lbl = QLabel("£0")
        self._report_outflows_lbl = QLabel("£0")
        self._report_closing_lbl = QLabel("£0")

        layout.addLayout(self._kv_row("Opening balance", self._report_opening_lbl))
        layout.addLayout(self._kv_row("Inflows", self._report_inflows_lbl,
                                       value_color="positive"))
        layout.addLayout(self._kv_row("Outflows", self._report_outflows_lbl,
                                       value_color="negative"))
        layout.addLayout(self._kv_row("Closing balance", self._report_closing_lbl,
                                       bold=True))
        return layout

    def _build_info_panel(self) -> QWidget:
        panel = self._make_card("infoCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        layout.addWidget(self._section_header("SUMMARY"))
        self._recorded_balance_lbl = QLabel("£0.00")
        self._scheduled_note_lbl = QLabel("No scheduled transactions for this account.")
        layout.addLayout(
            self._kv_row("Recorded Balance", self._recorded_balance_lbl, bold=True),
        )
        tokens.themed(self._scheduled_note_lbl, "color: {muted}; font-size: 12px;")
        layout.addWidget(self._scheduled_note_lbl)

        layout.addWidget(self._build_separator())

        layout.addWidget(self._section_header("ADDITIONAL INFO"))
        self._uncleared_lbl = QLabel("£0.00")
        self._cleared_lbl = QLabel("£0.00")
        self._uncleared_count_lbl = QLabel("Unconfirmed")
        layout.addLayout(self._kv_row_pair(
            self._uncleared_count_lbl, self._uncleared_lbl,
            value_color="negative",
        ))
        layout.addLayout(self._kv_row("Confirmed Balance", self._cleared_lbl))

        layout.addWidget(self._build_separator())

        layout.addWidget(self._section_header("UPCOMING"))
        self._upcoming_container = QVBoxLayout()
        self._upcoming_container.setContentsMargins(0, 0, 0, 0)
        self._upcoming_container.setSpacing(4)
        layout.addLayout(self._upcoming_container)
        self._upcoming_empty_lbl = QLabel("None upcoming.")
        tokens.themed(self._upcoming_empty_lbl, "color: {muted}; font-size: 12px;")
        layout.addWidget(self._upcoming_empty_lbl)

        layout.addStretch(1)

        # Reconcile placeholder — see ADR-033 §Reconcile entry point.
        layout.addWidget(self._build_reconcile_placeholder())
        return panel

    # ── holdings (ADR-044) ──

    _HOLDINGS_COLUMNS = [
        "Symbol", "Security", "Shares", "Avg cost", "Cost basis",
        "Last price", "Market value", "Unrealised gain",
    ]

    def _build_holdings_panel(self) -> QWidget:
        panel = self._make_card("holdingsCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        header_row = QHBoxLayout()
        title = QLabel("HOLDINGS")
        tokens.themed(title, "color: {muted}; letter-spacing: 1px; font-size: 12px;")
        header_row.addWidget(title)
        header_row.addStretch(1)
        self._holdings_totals_lbl = QLabel("")
        tokens.themed(self._holdings_totals_lbl, "color: {text}; font-size: 12px;")
        header_row.addWidget(self._holdings_totals_lbl)
        layout.addLayout(header_row)

        # Live search over symbol + name — portfolios can be long.
        self._holdings_search = QLineEdit()
        self._holdings_search.setPlaceholderText("Search securities…")
        self._holdings_search.setClearButtonEnabled(True)
        self._holdings_search.textChanged.connect(self._render_holdings_table)
        layout.addWidget(self._holdings_search)

        self._holdings_table = QTableWidget(0, len(self._HOLDINGS_COLUMNS))
        self._holdings_table.setHorizontalHeaderLabels(self._HOLDINGS_COLUMNS)
        self._holdings_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._holdings_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._holdings_table.verticalHeader().setVisible(False)
        self._holdings_table.setAlternatingRowColors(True)
        hh = self._holdings_table.horizontalHeader()
        for col in range(len(self._HOLDINGS_COLUMNS)):
            hh.setSectionResizeMode(
                col,
                QHeaderView.Stretch if col == 1 else QHeaderView.ResizeToContents,
            )
        layout.addWidget(self._holdings_table, stretch=1)
        return panel

    def _update_holdings_panel(self, view: HoldingsView) -> None:
        """Store the latest view, refresh the totals line, and (re)render the
        table through the current search filter."""
        self._holdings_view = view
        ccy = self._account.currency
        gain = view.total_unrealized_gain
        parts = [f"Account value {_fmt_ccy(view.account_value, ccy)}"]
        if view.holdings_market_value != 0 or view.total_unrealized_gain != 0:
            parts.append(f"Unrealised {_fmt_ccy(gain, ccy, signed=True)}")
        if view.total_realized_gain != 0:
            parts.append(f"Realised {_fmt_ccy(view.total_realized_gain, ccy, signed=True)}")
        parts.append(f"Cash {_fmt_ccy(view.cash_balance, ccy)}")
        self._holdings_totals_lbl.setText("   ·   ".join(parts))
        self._render_holdings_table()

    def _render_holdings_table(self) -> None:
        ccy = self._account.currency
        table = self._holdings_table
        view = self._holdings_view
        rows = list(view.holdings) if view is not None else []
        needle = self._holdings_search.text().strip().lower()
        if needle:
            rows = [
                h for h in rows
                if needle in h.symbol.lower() or needle in h.name.lower()
            ]
        table.setRowCount(len(rows))
        for i, h in enumerate(rows):
            name = h.name + (" *" if h.basis_incomplete else "")
            cells = [
                (h.symbol, Qt.AlignLeft),
                (name, Qt.AlignLeft),
                (_fmt_shares(h.shares), Qt.AlignRight),
                (_fmt_ccy(Decimal(str(h.avg_unit_cost)), ccy, decimals=4)
                 if h.avg_unit_cost is not None else "—", Qt.AlignRight),
                (_fmt_ccy(h.cost_basis, ccy), Qt.AlignRight),
                (_fmt_ccy(Decimal(str(h.last_price)), ccy, decimals=4)
                 if h.last_price is not None else "—", Qt.AlignRight),
                (_fmt_ccy(h.market_value, ccy) if h.market_value is not None else "—",
                 Qt.AlignRight),
                (_fmt_ccy(h.unrealized_gain, ccy, signed=True)
                 if h.unrealized_gain is not None else "—", Qt.AlignRight),
            ]
            for col, (text, align) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(int(align | Qt.AlignVCenter))
                if col == 7 and h.unrealized_gain is not None:
                    item.setForeground(QBrush(QColor(
                        tokens.c("positive") if h.unrealized_gain >= 0 else tokens.c("negative")
                    )))
                if col == 1 and h.basis_incomplete:
                    item.setToolTip(
                        "Cost basis is approximate — this holding includes "
                        "shares transferred in without a price, an unapplied "
                        "stock split, or an oversell."
                    )
                table.setItem(i, col, item)

    # ── portfolio panel (ADR-045): treemap (default) + cost-vs-value bars ──

    def _build_value_panel(self) -> QWidget:
        panel = self._make_card("portfolioCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        header_row = QHBoxLayout()
        header = QLabel("PORTFOLIO")
        tokens.themed(header, "color: {muted}; letter-spacing: 1px; font-size: 12px;")
        header_row.addWidget(header)
        header_row.addStretch(1)
        header_row.addWidget(QLabel("View:"))
        self._portfolio_view_combo = QComboBox()
        # Treemap is the default portfolio view; cost-vs-value bars are the
        # alternate. Index order MUST match the stack below.
        self._portfolio_view_combo.addItem("Treemap (allocation)")
        self._portfolio_view_combo.addItem("Cost vs value")
        self._portfolio_view_combo.currentIndexChanged.connect(
            self._on_portfolio_view_changed
        )
        header_row.addWidget(self._portfolio_view_combo)
        self._portfolio_total_chk = QCheckBox("Portfolio total")
        self._portfolio_total_chk.setToolTip(
            "Show one aggregate bar (total cost vs total value) instead of "
            "one bar per security."
        )
        self._portfolio_total_chk.toggled.connect(self._on_portfolio_total_toggled)
        self._portfolio_total_chk.hide()  # bars-only; treemap is the default view
        header_row.addWidget(self._portfolio_total_chk)
        self._securities_btn = QPushButton("Securities…")
        self._securities_btn.setToolTip("Choose which securities to show")
        self._securities_btn.clicked.connect(self._on_pick_securities)
        header_row.addWidget(self._securities_btn)
        layout.addLayout(header_row)

        self._portfolio_stack = QStackedWidget()
        self._treemap_chart = TreemapChart()
        self._value_chart = ValueChart()
        self._portfolio_stack.addWidget(self._treemap_chart)   # index 0 (default)
        self._portfolio_stack.addWidget(self._value_chart)     # index 1
        layout.addWidget(self._portfolio_stack, stretch=1)
        return panel

    def _on_portfolio_view_changed(self, index: int) -> None:
        self._portfolio_stack.setCurrentIndex(index)
        # The Portfolio-total toggle only applies to the bars view.
        self._portfolio_total_chk.setVisible(index == 1)

    def _render_portfolio(self) -> None:
        self._render_treemap()
        self._render_value_chart()

    def _selected_holdings(self):
        view = self._holdings_view
        if view is None:
            return []
        if self._chart_selected_ids is None:
            return list(view.holdings)
        return [h for h in view.holdings if h.security_id in self._chart_selected_ids]

    def _render_treemap(self) -> None:
        holdings = self._selected_holdings()
        if not holdings:
            self._treemap_chart.show_empty("No holdings to show.")
            return
        priced = [h for h in holdings if h.market_value is not None]
        if priced:
            tiles = [
                TreemapTile(label=(h.symbol or h.name[:8]), name=h.name,
                            value=float(h.market_value))
                for h in priced
            ]
            subtitle = "by market value"
            excluded = len(holdings) - len(priced)
            footnote = (
                f"{excluded} unpriced holding{'s' if excluded != 1 else ''} excluded"
                if excluded else ""
            )
        else:
            # Nothing priced yet — size by cost basis so the mix still renders.
            tiles = [
                TreemapTile(label=(h.symbol or h.name[:8]), name=h.name,
                            value=float(h.cost_basis))
                for h in holdings
            ]
            subtitle = "by cost basis (no prices yet — backfill in Manage ▸ Securities)"
            footnote = ""
        self._treemap_chart.render(
            tiles, _sym(self._account.currency), subtitle=subtitle, footnote=footnote,
        )

    def _value_bars(self, view: HoldingsView) -> list[ValueBar]:
        """Build the chart's bars from the holdings view, honouring the
        security selection and the portfolio-total toggle."""
        holdings = view.holdings
        if self._chart_selected_ids is not None:
            holdings = [h for h in holdings if h.security_id in self._chart_selected_ids]
        if self._chart_total_mode:
            cost = float(sum((h.cost_basis for h in holdings), Decimal("0")))
            priced = [h for h in holdings if h.market_value is not None]
            value = (
                float(sum((h.market_value for h in priced), Decimal("0")))
                if priced else None
            )
            if cost <= 0 and not value:
                return []
            return [ValueBar(security_id=-1, label="Portfolio", name="Portfolio total",
                             cost=cost, value=value)]
        bars = [
            ValueBar(
                security_id=h.security_id,
                label=(h.symbol or h.name[:8]),
                name=h.name,
                cost=float(h.cost_basis),
                value=float(h.market_value) if h.market_value is not None else None,
            )
            for h in holdings
        ]
        bars.sort(key=lambda b: -(b.value if b.value is not None else b.cost))
        return bars

    def _render_value_chart(self) -> None:
        view = self._holdings_view
        if view is None or not view.holdings:
            self._value_chart.show_empty("No holdings yet.")
            return
        bars = self._value_bars(view)
        if not bars:
            self._value_chart.show_empty("No securities selected.")
            return
        self._value_chart.render(bars, _sym(self._account.currency))

    def _render_value_history(self, txns) -> None:
        """Portfolio value over time (ADR-045): monthly invested-vs-market-value
        line, full history. Loads each referenced security's price series once."""
        dated = [t for t in txns if t.posted_date]
        if len(dated) < 2:
            self._value_history_chart.show_empty("Not enough history to chart yet.")
            return
        first = min(t.posted_date for t in dated)
        samples = _month_end_samples(first, date.today())
        sec_ids = {t.security_id for t in txns if t.security_id is not None}
        price_series = {
            sid: [(p.price_date, p.price) for p in self._repo.price_series(sid)]
            for sid in sec_ids
        }
        points = compute_value_history(
            txns, samples, price_series, self._repo.security_multipliers(),
        )
        if len(points) < 2:
            self._value_history_chart.show_empty("Not enough history to chart yet.")
            return
        any_fallback = any(not p.fully_priced for p in points)
        self._value_history_chart.render(points, _sym(self._account.currency), any_fallback)

    def _on_portfolio_total_toggled(self, checked: bool) -> None:
        self._chart_total_mode = checked
        self._securities_btn.setEnabled(not checked)
        self._render_value_chart()

    def _on_pick_securities(self) -> None:
        view = self._holdings_view
        if view is None or not view.holdings:
            return
        rows = [(h.security_id, h.symbol or h.name) for h in view.holdings]
        dialog = _SecurityPickerDialog(rows, self._chart_selected_ids, parent=self)
        if dialog.exec() == QDialog.Accepted:
            picked = dialog.selected_ids()
            all_ids = {sid for sid, _ in rows}
            # None == "all" so the chart stays in sync as holdings change.
            self._chart_selected_ids = None if picked == all_ids else picked
            self._render_portfolio()

    def _build_top_n_panel(self, *, title: str, widget_attr: str) -> QWidget:
        # Card name picked off the widget attr so each Top-N card has a
        # distinct objectName for QSS scoping.
        card_name = f"topNCard_{widget_attr.strip('_')}"
        panel = self._make_card(card_name)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        layout.addWidget(self._section_header(title))
        list_widget = _TopNList()
        setattr(self, widget_attr, list_widget)
        layout.addWidget(list_widget, stretch=1)
        return panel

    def _build_reconcile_placeholder(self) -> QWidget:
        """Banktivity-style statement row (ADR-040). The status text reflects
        the account's statement state; the button opens the statement history
        (:class:`StatementsWindow`)."""
        row = QFrame()
        tokens.themed(row, "QFrame { border: 1px solid {border}; border-radius: 6px; background-color: {surface_alt}; }")
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 8, 10, 8)
        h.setSpacing(8)

        self._statements_status_lbl = QLabel("NO STATEMENTS")
        tokens.themed(self._statements_status_lbl, "color: {muted}; letter-spacing: 1px; font-size: 12px; background: transparent; border: none;")
        h.addWidget(self._statements_status_lbl)
        h.addStretch(1)

        reconcile_btn = QPushButton("RECONCILE ›")
        tokens.themed(reconcile_btn, "QPushButton { color: {accent}; background: transparent; border: none; font-weight: bold; font-size: 12px; }QPushButton:hover { color: {accent_hover}; }")
        reconcile_btn.setCursor(Qt.PointingHandCursor)
        reconcile_btn.clicked.connect(self._on_reconcile_clicked)
        h.addWidget(reconcile_btn)
        self._refresh_statements_row()
        return row

    def _refresh_statements_row(self) -> None:
        """Update the statement-row status text from the account's statements.
        Safe to call before the row is built (guards on the attribute)."""
        if not hasattr(self, "_statements_status_lbl"):
            return
        statements = self._repo.list_statements_for_account(self._account_id)
        if not statements:
            self._statements_status_lbl.setText("NO STATEMENTS")
            return
        open_stmt = next((s for s in statements if s.status == "open"), None)
        if open_stmt is not None:
            d = date.fromisoformat(open_stmt.end_date)
            self._statements_status_lbl.setText(
                f"IN PROGRESS · {d.day} {d.strftime('%b %Y')}"
            )
            return
        out = sum(1 for s in statements if s.is_out_of_balance)
        last = statements[0]  # newest end_date first
        d = date.fromisoformat(last.end_date)
        text = f"LAST RECONCILED · {d.day} {d.strftime('%b %Y')}"
        if out:
            text += f" · {out} OUT OF BALANCE"
        self._statements_status_lbl.setText(text)

    def _build_separator(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        return sep

    def _section_header(self, text: str) -> QLabel:
        lbl = QLabel(text)
        tokens.themed(lbl, "color: {muted}; letter-spacing: 1px; font-size: 12px;")
        return lbl

    def _kv_row(
        self,
        key: str,
        value_lbl: QLabel,
        *,
        bold: bool = False,
        value_color: Optional[str] = None,
    ) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        key_lbl = QLabel(key)
        tokens.themed(key_lbl, "color: {muted};")
        h.addWidget(key_lbl)
        h.addStretch(1)
        if value_color:
            tokens.themed(value_lbl, "color: {%s};" % value_color)
        else:
            tokens.themed(value_lbl, "color: {text};")
        if bold:
            f = value_lbl.font()
            f.setBold(True)
            value_lbl.setFont(f)
        value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(value_lbl)
        return h

    def _kv_row_pair(
        self,
        key_lbl: QLabel,
        value_lbl: QLabel,
        *,
        value_color: Optional[str] = None,
    ) -> QHBoxLayout:
        """Like ``_kv_row`` but the key is a live QLabel the reload code
        rewrites (so the count in 'Unconfirmed (3)' updates)."""
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        tokens.themed(key_lbl, "color: {muted};")
        h.addWidget(key_lbl)
        h.addStretch(1)
        if value_color:
            tokens.themed(value_lbl, "color: {%s};" % value_color)
        else:
            tokens.themed(value_lbl, "color: {text};")
        value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(value_lbl)
        return h

    # ── refresh ──

    def event(self, ev):
        # Cheap enough at MFL's scale to re-query on every activation —
        # same pattern as BudgetWindow (ADR-024).
        if ev.type() == QEvent.WindowActivate:
            self.reload()
        return super().event(ev)

    def reload(self) -> None:
        # During app shutdown the owning RegisterWindow may already have closed
        # the shared repository (ADR-057/109) while a queued WindowActivate still
        # fires here — querying a closed connection raises
        # ``sqlite3.ProgrammingError: Cannot operate on a closed database`` and
        # crashes the quit. Bail out quietly, the same guard BudgetWindow /
        # HomeView use on their activate-refresh.
        if not self._repo.is_open():
            return
        # Re-pull the account in case the name changed (rename via the
        # Accounts dialog while this window was open).
        account = self._repo.get_account_by_id(self._account_id)
        if account is None:
            # Account got deleted out from under us. Close gracefully.
            self.close()
            return
        self._account = account
        self.setWindowTitle(f"{account.name} · Summary")
        # A loan has its own self-contained view (ADR-095) — none of the
        # cash/investment info-panel, statements, or Top-N widgets exist.
        if self._is_loan:
            self._loan_view.reload()
            return
        self._refresh_statements_row()

        txns = self._repo.list_transactions_for_account(self._account_id)
        opening_balance = self._account.opening_balance
        today = date.today()

        # Shared info-panel feeds (both layouts): status breakdown + the
        # account's scheduled/upcoming transactions.
        status_breakdown = compute_status_breakdown(txns, opening_balance)
        horizon = today.toordinal() + 30
        through_date = date.fromordinal(horizon).isoformat()
        all_schedules = self._repo.list_schedules_due_through(through_date)
        upcoming_rows = upcoming_scheduled(
            all_schedules, self._account_id, today, horizon_days=30, n=5,
        )
        scheduled_total = count_scheduled_for_account(
            self._repo.list_scheduled_txns(), self._account_id,
        )
        self._update_summary_panel(status_breakdown, scheduled_total)
        self._update_upcoming(upcoming_rows, today)

        if self._is_investment:
            self._reload_investment(txns, opening_balance)
        else:
            self._reload_cash(txns, opening_balance, today)

    def _reload_investment(self, txns, opening_balance) -> None:
        """Investment dashboard refresh (ADR-044/045): holdings → value chart
        + Holdings table + the unpriced banner. No cash-flow chart / report /
        period / Top-N widgets exist on this layout."""
        prices = {
            sid: (p.price, p.price_date)
            for sid, p in self._repo.latest_prices().items()
        }
        view = compute_holdings_view(
            txns, opening_balance, prices, self._repo.security_multipliers(),
        )
        self._update_holdings_panel(view)   # stores view + renders table (search-aware)
        self._render_portfolio()            # treemap (default) + cost-vs-value bars
        self._render_value_history(txns)
        if view.unpriced_count:
            self._non_cash_banner.setText(
                f"{view.unpriced_count} holding"
                f"{'s' if view.unpriced_count != 1 else ''} unpriced — "
                "add prices in Manage ▸ Securities for full market value."
            )
            self._non_cash_banner.show()
        else:
            self._non_cash_banner.hide()

    def _reload_cash(self, txns, opening_balance, today) -> None:
        """Cash-account single-page refresh (ADR-033/034): flow chart + report
        panel + Top-N breakdowns."""
        period_start, period_end = self._resolve_period_bounds()
        period_label = self._period_display_label()
        period_days = max(1, (period_end - period_start).days)
        granularity = pick_granularity(period_days)

        flow_series = compute_balance_flow_series(
            txns, opening_balance, period_start, period_end, granularity,
        )
        period_summary = compute_period_summary(
            txns, opening_balance, period_start, period_end, period_label,
        )
        in_period_txns = [
            t for t in txns
            if period_start.isoformat() <= t.posted_date <= period_end.isoformat()
        ]
        payees_rows = top_payees(in_period_txns, n=10)
        # Unroll split transactions (ADR-051) so each split line lands on its
        # own category rather than the parent's Uncategorised bucket.
        split_ids = [t.id for t in in_period_txns if t.split_count]
        split_lines_by_txn = self._repo.split_lines_for_txns(split_ids)
        categories_rows = top_categories(
            in_period_txns, n=10, split_lines_by_txn=split_lines_by_txn,
        )

        if not txns or not flow_series.buckets:
            self._chart.show_empty("Not enough history yet")
        else:
            self._chart.set_data(flow_series)

        self._update_report_panel(period_summary)
        self._top_payees_widget.set_rows(payees_rows)
        self._top_categories_widget.set_rows(categories_rows)
        # Non-cash banner — property / vehicle until valuations land.
        self._non_cash_banner.setVisible(
            self._account.family in _NON_CASH_FAMILIES
        )

    def _update_report_panel(self, summary: PeriodSummary) -> None:
        self._report_header.setText(f"REPORT: {summary.period_label.upper()}")
        self._report_opening_lbl.setText(_fmt_money_no_decimals(summary.opening_balance))
        self._report_inflows_lbl.setText(_fmt_money_no_decimals(summary.inflows))
        if summary.outflows > 0:
            self._report_outflows_lbl.setText(_fmt_money_no_decimals(-summary.outflows))
        else:
            self._report_outflows_lbl.setText("£0")
        self._report_closing_lbl.setText(_fmt_money_no_decimals(summary.closing_balance))

    def _update_summary_panel(
        self, breakdown: StatusBreakdown, scheduled_total: int,
    ) -> None:
        self._recorded_balance_lbl.setText(_fmt_money(breakdown.recorded_balance))
        if scheduled_total == 0:
            self._scheduled_note_lbl.setText(
                "No scheduled transactions for this account."
            )
        elif scheduled_total == 1:
            self._scheduled_note_lbl.setText(
                "1 scheduled transaction for this account."
            )
        else:
            self._scheduled_note_lbl.setText(
                f"{scheduled_total} scheduled transactions for this account."
            )
        if breakdown.uncleared_count > 0:
            self._uncleared_count_lbl.setText(
                f"Unconfirmed ({breakdown.uncleared_count})"
            )
            self._uncleared_lbl.setText(_fmt_money(breakdown.uncleared_amount))
        else:
            self._uncleared_count_lbl.setText("Unconfirmed")
            self._uncleared_lbl.setText("£0.00")
        self._cleared_lbl.setText(_fmt_money(breakdown.cleared_balance))

    def _update_upcoming(
        self, rows: list[UpcomingScheduled], today: date,
    ) -> None:
        # Clear the live container; rebuild from rows. Keeping the empty
        # label outside the container makes show/hide trivial.
        while self._upcoming_container.count():
            item = self._upcoming_container.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                inner = item.layout()
                if inner is not None:
                    self._drop_layout(inner)
        if not rows:
            horizon_label = date.fromordinal(today.toordinal() + 30)
            self._upcoming_empty_lbl.setText(
                f"None through {horizon_label.day} {horizon_label.strftime('%b %Y')}."
            )
            self._upcoming_empty_lbl.show()
            return
        self._upcoming_empty_lbl.hide()
        for row in rows:
            self._upcoming_container.addLayout(self._build_upcoming_row(row))

    def _build_upcoming_row(self, row: UpcomingScheduled) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        when = QLabel(self._describe_when(row.days_until, row.next_due_date))
        tokens.themed(when, "color: {muted}; font-size: 12px;")
        when.setFixedWidth(86)
        h.addWidget(when)

        label = QLabel(row.label)
        tokens.themed(label, "color: {text};")
        h.addWidget(label)
        h.addStretch(1)

        amt = QLabel(_fmt_money(row.amount))
        if row.amount >= 0:
            tokens.themed(amt, "color: {positive};")
        else:
            tokens.themed(amt, "color: {negative};")
        amt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(amt)
        return h

    @staticmethod
    def _describe_when(days_until: int, due_iso: str) -> str:
        if days_until == 0:
            return "Today"
        if days_until == 1:
            return "Tomorrow"
        if days_until < 0:
            return f"{-days_until}d ago"
        if days_until <= 14:
            return f"in {days_until}d"
        d = date.fromisoformat(due_iso)
        return f"{d.day} {d.strftime('%b')}"

    def _drop_layout(self, layout) -> None:
        """Recursively delete a layout's widgets — Qt has no one-liner for
        this and the upcoming-row layouts contain a few QLabels each."""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                inner = item.layout()
                if inner is not None:
                    self._drop_layout(inner)

    # ── handlers ──

    def _on_period_selected(self, key: str) -> None:
        if key not in PERIOD_KEYS:
            return
        if key == "custom":
            # Open the dialog seeded with the currently-displayed range
            # so Custom is "edit where you are" rather than "start over."
            today = date.today()
            if self._custom_start is not None and self._custom_end is not None:
                seed_from, seed_to = self._custom_start, self._custom_end
            elif self._previous_period != "custom":
                seed_from, seed_to = period_bounds(self._previous_period, today)
            else:
                # No previous non-custom selection (would be weird, but
                # defensive) — default to last 90 days.
                seed_from, seed_to = period_bounds("quarter", today)
            dialog = CustomPeriodDialog(
                initial_from=seed_from, initial_to=seed_to, parent=self,
            )
            if dialog.exec() != CustomPeriodDialog.Accepted:
                # User cancelled — restore the previously-checked button.
                self._period_buttons[self._previous_period].setChecked(True)
                return
            self._custom_start, self._custom_end = dialog.values()
        # Common path — accepted custom, or any non-custom preset.
        self._previous_period = self._current_period
        self._current_period = key
        self.reload()

    def _resolve_period_bounds(self) -> tuple[date, date]:
        """Return the active period's (start, end), honouring a custom
        range when one is set. Wrapping ``period_bounds`` here keeps the
        custom-dispatch in one place instead of every caller."""
        today = date.today()
        if self._current_period == "custom":
            if self._custom_start is None or self._custom_end is None:
                # Shouldn't happen — the dialog only sets the key on
                # Accepted — but if it does, fall back to last quarter so
                # the screen renders something reasonable.
                return period_bounds("quarter", today)
            return self._custom_start, self._custom_end
        return period_bounds(self._current_period, today)

    def _period_display_label(self) -> str:
        return period_display_label(
            self._current_period, self._custom_start, self._custom_end,
        )

    def _on_reconcile_clicked(self) -> None:
        dialog = StatementsWindow(self._repo, self._account, parent=self)
        dialog.statements_changed.connect(self._refresh_statements_row)
        dialog.exec()
        # Statuses / balances may have changed (a close stamps rows
        # Reconciled); re-pull everything on this screen.
        self.reload()

    # ── drill-down to TransactionsListWindow (ADR-034) ──

    def _on_payee_clicked(self, row: TopNRow) -> None:
        if row.entity_id is None:
            return
        tf = TxnListFilter.for_payee(
            account_id=self._account.id,
            account_name=self._account.name,
            payee_id=row.entity_id,
            payee_label=row.label,
            period_key=self._current_period,
            custom_start=self._custom_start,
            custom_end=self._custom_end,
        )
        self._open_or_raise_drilldown(tf)

    def _on_category_clicked(self, row: TopNRow) -> None:
        if row.entity_id is None:
            return
        tf = TxnListFilter.for_category(
            account_id=self._account.id,
            account_name=self._account.name,
            category_id=row.entity_id,
            category_label=row.label,
            period_key=self._current_period,
            custom_start=self._custom_start,
            custom_end=self._custom_end,
        )
        self._open_or_raise_drilldown(tf)

    def _open_or_raise_drilldown(self, tf: TxnListFilter) -> None:
        key = tf.signature()
        existing = self._drilldown_wins.get(key)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        win = TransactionsListWindow(self._repo, tf, parent=self)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(
            lambda _obj=None, k=key: self._on_drilldown_closed(k)
        )
        self._drilldown_wins[key] = win
        win.show()

    def _on_drilldown_closed(self, key: tuple) -> None:
        self._drilldown_wins.pop(key, None)


class _SecurityPickerDialog(QDialog):
    """Popup wrapping the reusable CheckListPanel so the value chart can be
    narrowed to any subset of securities (ADR-045). ``selected_ids`` seeds the
    checked subset; None means all checked."""

    def __init__(
        self,
        rows: list[tuple[int, str]],
        selected_ids: Optional[set[int]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose securities")
        self.setMinimumSize(360, 480)
        layout = QVBoxLayout(self)
        self._panel = CheckListPanel(
            "Securities", rows, placeholder="Search securities…",
        )
        if selected_ids is not None:
            self._panel.set_checked_ids(selected_ids)
        layout.addWidget(self._panel, stretch=1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_ids(self) -> set[int]:
        return set(self._panel.checked_ids())
