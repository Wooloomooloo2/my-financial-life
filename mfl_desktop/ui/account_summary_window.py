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

from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

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
from mfl_desktop.ui.transactions_list_window import (
    TransactionsListWindow,
    TxnListFilter,
)


# Tailwind v3 vocabulary — kept local because these are screen-level
# accents rather than chart series colours.
_COLOR_HEADING   = "#6b7280"   # slate-500 — section header text
_COLOR_BODY      = "#111827"   # slate-900 — main values
_COLOR_MUTED     = "#9ca3af"   # slate-400
_COLOR_POSITIVE  = "#16a34a"   # green-600
_COLOR_NEGATIVE  = "#dc2626"   # red-600
_COLOR_ACCENT    = "#2563eb"   # blue-600 — clickable Reconcile link
_COLOR_BAR_FILL  = "#bfdbfe"   # blue-200 — soft fill for the top-N bar
_COLOR_BAR_TRACK = "#f1f5f9"   # slate-100 — track behind the bar
_COLOR_ROW_HOVER = "#f1f5f9"   # slate-100 — Top-N hover tint (ADR-034)
# Section card palette (ADR-034 §2): cards float on a slate-50 canvas
# with a soft slate-200 border so the screen reads as a grid of units.
_COLOR_CARD_BG     = "#ffffff"
_COLOR_CARD_BORDER = "#e5e7eb"
_COLOR_WINDOW_BG   = "#f8fafc"

_DEFAULT_PERIOD = "quarter"   # rolling 90 days — replaces the old "90d" key
_NON_CASH_FAMILIES = {"investment", "property", "vehicle"}


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
        painter.fillRect(self.rect(), QColor("#ffffff"))

        self._hitmap.clear()

        if not self._rows:
            painter.setPen(QPen(QColor(_COLOR_MUTED)))
            font = QFont(painter.font())
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(
                self.rect(), Qt.AlignCenter, self._empty_message,
            )
            painter.end()
            return

        n = len(self._rows)
        row_h = max(22, min(34, int(self.height() / max(n, 1))))
        font = QFont(painter.font())
        font.setPointSize(10)
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
                painter.setBrush(QBrush(QColor(_COLOR_ROW_HOVER)))
                painter.drawRect(row_rect)

            # Label (left).
            painter.setPen(QPen(QColor(_COLOR_BODY)))
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
            painter.setBrush(QBrush(QColor(_COLOR_BAR_TRACK)))
            painter.drawRoundedRect(track, 4, 4)
            fill_w = max(0.0, bar_w_total * max(0.0, min(1.0, row.proportion)))
            if fill_w > 0:
                fill = QRectF(bar_x, bar_y, fill_w, 8)
                painter.setBrush(QBrush(QColor(_COLOR_BAR_FILL)))
                painter.drawRoundedRect(fill, 4, 4)

            # Amount (right).
            painter.setPen(QPen(QColor(_COLOR_BODY)))
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

        # ── widgets ──
        chart_panel = self._build_chart_panel()
        info_panel = self._build_info_panel()
        top_payees_panel = self._build_top_n_panel(
            title="TOP PAYEES (this period)", widget_attr="_top_payees_widget",
        )
        top_categories_panel = self._build_top_n_panel(
            title="TOP CATEGORIES (this period)",
            widget_attr="_top_categories_widget",
        )

        # Wire Top-N clicks → drill-down (ADR-034).
        self._top_payees_widget.row_clicked.connect(self._on_payee_clicked)
        self._top_categories_widget.row_clicked.connect(self._on_category_clicked)

        top_split = QSplitter(Qt.Horizontal)
        top_split.addWidget(chart_panel)
        top_split.addWidget(info_panel)
        top_split.setStretchFactor(0, 3)
        top_split.setStretchFactor(1, 2)
        top_split.setSizes([720, 480])
        # Wider handles so the cards have a visible gutter, not a hairline
        # touch — matches the card aesthetic (ADR-034 §2).
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

        container = QWidget()
        container.setObjectName("summaryRoot")
        container.setStyleSheet(
            f"QWidget#summaryRoot {{ background-color: {_COLOR_WINDOW_BG}; }}"
        )
        v = QVBoxLayout(container)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)
        v.addWidget(self._build_title())
        v.addWidget(outer, stretch=1)
        self.setCentralWidget(container)

        self.reload()

    # ── builders ──

    def _build_title(self) -> QWidget:
        title = QLabel(self._account.name)
        f = title.font()
        f.setPointSize(f.pointSize() + 8)
        f.setBold(True)
        title.setFont(f)
        title.setStyleSheet(f"color: {_COLOR_BODY};")
        return title

    def _make_card(self, name: str) -> QFrame:
        """QFrame styled as a card (ADR-034 §2). The objectName scopes
        the rounded border + background so child widgets don't inherit
        it. Child QFrames (e.g. the inline separators) need to disable
        their own border to look right inside a card."""
        card = QFrame()
        card.setObjectName(name)
        card.setStyleSheet(
            f"QFrame#{name} {{ background-color: {_COLOR_CARD_BG}; "
            f"border: 1px solid {_COLOR_CARD_BORDER}; border-radius: 10px; }}"
        )
        return card

    def _build_chart_panel(self) -> QWidget:
        panel = self._make_card("chartCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        header = QLabel("ACCOUNT BALANCE")
        header.setStyleSheet(
            f"color: {_COLOR_HEADING}; letter-spacing: 1px; font-size: 9pt;"
        )
        layout.addWidget(header)

        # Banner for non-cash families: investment / property / vehicle.
        # Hidden by default; toggled in reload() based on account.family.
        self._non_cash_banner = QLabel(
            "Balance reflects recorded transactions; valuations not yet wired."
        )
        self._non_cash_banner.setStyleSheet(
            "color: #92400e; background-color: #fef3c7; "
            "padding: 6px 10px; border-radius: 4px; font-size: 9pt;"
        )
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
            btn.setStyleSheet(
                "QPushButton { padding: 5px 12px; border: 1px solid #cbd5e1; "
                "border-radius: 14px; background-color: #ffffff; "
                "color: #334155; font-size: 9pt; }"
                "QPushButton:checked { background-color: #2563eb; "
                "color: #ffffff; border-color: #2563eb; font-weight: bold; }"
                "QPushButton:hover:!checked { background-color: #f1f5f9; }"
            )
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
        self._report_header.setStyleSheet(
            f"color: {_COLOR_HEADING}; letter-spacing: 1px; font-size: 9pt;"
        )
        layout.addWidget(self._report_header)

        self._report_opening_lbl = QLabel("£0")
        self._report_inflows_lbl = QLabel("£0")
        self._report_outflows_lbl = QLabel("£0")
        self._report_closing_lbl = QLabel("£0")

        layout.addLayout(self._kv_row("Opening balance", self._report_opening_lbl))
        layout.addLayout(self._kv_row("Inflows", self._report_inflows_lbl,
                                       value_color=_COLOR_POSITIVE))
        layout.addLayout(self._kv_row("Outflows", self._report_outflows_lbl,
                                       value_color=_COLOR_NEGATIVE))
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
        self._scheduled_note_lbl.setStyleSheet(f"color: {_COLOR_HEADING}; font-size: 9pt;")
        layout.addWidget(self._scheduled_note_lbl)

        layout.addWidget(self._build_separator())

        layout.addWidget(self._section_header("ADDITIONAL INFO"))
        self._uncleared_lbl = QLabel("£0.00")
        self._cleared_lbl = QLabel("£0.00")
        self._uncleared_count_lbl = QLabel("Uncleared")
        layout.addLayout(self._kv_row_pair(
            self._uncleared_count_lbl, self._uncleared_lbl,
            value_color=_COLOR_NEGATIVE,
        ))
        layout.addLayout(self._kv_row("Cleared Balance", self._cleared_lbl))

        layout.addWidget(self._build_separator())

        layout.addWidget(self._section_header("UPCOMING"))
        self._upcoming_container = QVBoxLayout()
        self._upcoming_container.setContentsMargins(0, 0, 0, 0)
        self._upcoming_container.setSpacing(4)
        layout.addLayout(self._upcoming_container)
        self._upcoming_empty_lbl = QLabel("None upcoming.")
        self._upcoming_empty_lbl.setStyleSheet(
            f"color: {_COLOR_HEADING}; font-size: 9pt;"
        )
        layout.addWidget(self._upcoming_empty_lbl)

        layout.addStretch(1)

        # Reconcile placeholder — see ADR-033 §Reconcile entry point.
        layout.addWidget(self._build_reconcile_placeholder())
        return panel

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
        """Banktivity-style 'NO STATEMENTS · RECONCILE ›' row. Click opens
        an info dialog explaining the feature is on the way."""
        row = QFrame()
        row.setStyleSheet(
            "QFrame { border: 1px solid #e5e7eb; border-radius: 6px; "
            "background-color: #fafafa; }"
        )
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 8, 10, 8)
        h.setSpacing(8)

        status = QLabel("NO STATEMENTS")
        status.setStyleSheet(
            "color: #6b7280; letter-spacing: 1px; font-size: 9pt; "
            "background: transparent; border: none;"
        )
        h.addWidget(status)
        h.addStretch(1)

        reconcile_btn = QPushButton("RECONCILE ›")
        reconcile_btn.setStyleSheet(
            "QPushButton { color: #2563eb; background: transparent; "
            "border: none; font-weight: bold; font-size: 9pt; }"
            "QPushButton:hover { color: #1d4ed8; }"
        )
        reconcile_btn.setCursor(Qt.PointingHandCursor)
        reconcile_btn.clicked.connect(self._on_reconcile_clicked)
        h.addWidget(reconcile_btn)
        return row

    def _build_separator(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        return sep

    def _section_header(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {_COLOR_HEADING}; letter-spacing: 1px; font-size: 9pt;"
        )
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
        key_lbl.setStyleSheet(f"color: {_COLOR_HEADING};")
        h.addWidget(key_lbl)
        h.addStretch(1)
        if value_color:
            value_lbl.setStyleSheet(f"color: {value_color};")
        else:
            value_lbl.setStyleSheet(f"color: {_COLOR_BODY};")
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
        rewrites (so the count in 'Uncleared (3)' updates)."""
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        key_lbl.setStyleSheet(f"color: {_COLOR_HEADING};")
        h.addWidget(key_lbl)
        h.addStretch(1)
        if value_color:
            value_lbl.setStyleSheet(f"color: {value_color};")
        else:
            value_lbl.setStyleSheet(f"color: {_COLOR_BODY};")
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
        # Re-pull the account in case the name changed (rename via the
        # Accounts dialog while this window was open).
        account = self._repo.get_account_by_id(self._account_id)
        if account is None:
            # Account got deleted out from under us. Close gracefully.
            self.close()
            return
        self._account = account
        self.setWindowTitle(f"{account.name} · Summary")

        txns = self._repo.list_transactions_for_account(self._account_id)
        opening_balance = self._account.opening_balance
        today = date.today()
        period_start, period_end = self._resolve_period_bounds()
        period_label = self._period_display_label()

        # Granularity from the SELECTED period's span — drives whether
        # the chart shows daily / weekly / monthly / quarterly / yearly
        # buckets. Works for fixed presets AND custom ranges.
        period_days = max(1, (period_end - period_start).days)
        granularity = pick_granularity(period_days)

        flow_series = compute_balance_flow_series(
            txns, opening_balance, period_start, period_end, granularity,
        )
        period_summary = compute_period_summary(
            txns, opening_balance, period_start, period_end, period_label,
        )
        status_breakdown = compute_status_breakdown(txns, opening_balance)

        in_period_txns = [
            t for t in txns
            if period_start.isoformat() <= t.posted_date <= period_end.isoformat()
        ]
        payees_rows = top_payees(in_period_txns, n=10)
        categories_rows = top_categories(in_period_txns, n=10)

        # Scheduled feeds — only schedules touching this account.
        horizon = today.toordinal() + 30
        through_date = date.fromordinal(horizon).isoformat()
        all_schedules = self._repo.list_schedules_due_through(through_date)
        upcoming_rows = upcoming_scheduled(
            all_schedules, self._account_id, today, horizon_days=30, n=5,
        )
        # Total scheduled count uses the full schedule list (no horizon).
        scheduled_total = count_scheduled_for_account(
            self._repo.list_scheduled_txns(), self._account_id,
        )

        # Sparse-data signal — let the chart paint its empty state but
        # keep the rest of the screen useful. Empty txn list means the
        # account is brand new; an empty bucket list means a degenerate
        # period (e.g. "All time" on an account with no history → today).
        if not txns or not flow_series.buckets:
            self._chart.show_empty("Not enough history yet")
        else:
            self._chart.set_data(flow_series)

        self._update_report_panel(period_summary)
        self._update_summary_panel(status_breakdown, scheduled_total)
        self._update_upcoming(upcoming_rows, today)
        self._top_payees_widget.set_rows(payees_rows)
        self._top_categories_widget.set_rows(categories_rows)

        # Non-cash banner — investment / property / vehicle until valuations
        # land. ADR-032 / ADR-033 §Balance semantics for non-cash families.
        self._non_cash_banner.setVisible(self._account.family in _NON_CASH_FAMILIES)

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
                f"Uncleared ({breakdown.uncleared_count})"
            )
            self._uncleared_lbl.setText(_fmt_money(breakdown.uncleared_amount))
        else:
            self._uncleared_count_lbl.setText("Uncleared")
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
        when.setStyleSheet(f"color: {_COLOR_HEADING}; font-size: 9pt;")
        when.setFixedWidth(86)
        h.addWidget(when)

        label = QLabel(row.label)
        label.setStyleSheet(f"color: {_COLOR_BODY};")
        h.addWidget(label)
        h.addStretch(1)

        amt = QLabel(_fmt_money(row.amount))
        if row.amount >= 0:
            amt.setStyleSheet(f"color: {_COLOR_POSITIVE};")
        else:
            amt.setStyleSheet(f"color: {_COLOR_NEGATIVE};")
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
        QMessageBox.information(
            self,
            "Reconciliation",
            "Statement reconciliation is coming in a future release.\n\n"
            "When it lands, this button will open the reconcile flow for "
            f"{self._account.name}.",
        )

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
