"""Net Worth — assets vs debts at a glance.

Three columns: Summary on the left (net-worth total + two **two-ring donut
charts** — a larger Assets donut and a smaller Debts donut — plus a
colour-coded legend), Assets in the middle (grouped by family with each
account listed), Debts on the right (mirror of Assets in red). + Asset
and + Debt buttons at the bottom of their columns open the existing
AccountDialog so adding shows up in the report on accept.

The donuts' inner ring is account *type* and the outer ring is the
individual accounts within each type (ADR-067 — an owner-approved exception
to the ADR-018 "no pies" rule for this point-in-time composition view;
debts get their own donut because a donut can't hold a negative slice).
Investment balances are market value (cash + Σ shares × latest price, ADR-044);
property/vehicle use the cash formula until the valuation pipeline lands.

**Currency (ADR-055):** account values come back in each account's *own*
currency. Everything is converted to a chosen **display currency** (a selector,
default = the person's base currency) via ``Repository.convert_amount`` before
any total is summed — otherwise a USD brokerage would be added to GBP at par.
An account whose rate is missing is *excluded* from the totals (never folded in
at 1:1) and surfaced in a banner with a one-click path to set the rate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.account_types import ACCOUNT_TYPES
from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.ui.account_dialog import AccountDialog
from mfl_desktop.ui.currencies_dialog import CurrenciesDialog
from mfl_desktop.ui.donut_chart import DonutChart, DonutChild, DonutSegment
from mfl_desktop.ui.net_worth_history_chart import NetWorthHistoryChart
from mfl_desktop.ui.page_header import PageHeader
from mfl_desktop.ui import tokens
from mfl_desktop.net_worth_history import (
    gather_net_worth_history, period_end_samples, resolve_history_granularity,
)
from mfl_desktop.ui.chart_helpers import currency_symbol
from mfl_desktop.ui.date_widgets import make_date_edit
from mfl_desktop.ui.report_filter_dialog_base import GRANULARITY_OPTIONS


# Family → (display label, color, kind) where kind ∈ {"asset","debt"}.
# Order in this list is the display order in the summary + columns.
_FAMILY_VIEW: list[tuple[str, str, QColor, str]] = [
    ("investment", "Investments",   QColor("#2563eb"), "asset"),
    ("property",   "Property",      QColor("#14b8a6"), "asset"),
    ("vehicle",    "Vehicles",      QColor("#f59e0b"), "asset"),
    ("cash",       "Cash & Bank",   QColor("#22c55e"), "asset"),
    ("credit",     "Credit Cards",  QColor("#ec4899"), "debt"),
    ("loan",       "Loans",         QColor("#a855f7"), "debt"),
]

_ASSET_COLOR = QColor("#16a34a")   # column header
_DEBT_COLOR = QColor("#dc2626")

def _symbol(currency: str) -> str:
    """The currency glyph, via the one definition (ADR-165)."""
    return currency_symbol(currency) if currency else ""


@dataclass(frozen=True)
class _FamilyTotal:
    family: str
    label: str
    color: QColor
    kind: str  # "asset" | "debt"
    accounts: list[AccountSummary]
    total: Decimal


@dataclass(frozen=True)
class _TypeTotal:
    """One account-type row inside an Assets/Debts column. The colour is
    the type's family colour so visual identity is preserved with the
    summary panel's legend."""
    type_storage: str       # 'cash_std', etc.
    type_label: str         # 'Current account', etc.
    family: str
    color: QColor
    kind: str               # 'asset' | 'debt'
    accounts: list[AccountSummary]
    total: Decimal


class NetWorthWindow(QMainWindow):
    # Emitted when an outer-ring account slice is clicked (ADR-083) — the
    # register window opens that account's Summary via its canonical
    # single-instance path.
    account_activated = Signal(int)

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Net Worth")
        self.resize(1240, 720)
        self._repo = repo

        # Currency state, set per refresh.
        self._display_ccy: str = "GBP"
        self._converted: dict[int, Optional[Decimal]] = {}
        self._native: dict[int, Decimal] = {}
        self._missing: list[tuple[AccountSummary, Decimal]] = []
        self._fallback_used = False
        # Closed accounts are excluded by default (ADR-069); the toggle below
        # re-includes their balances in every total + the donut + columns.
        self._include_closed = False

        # Which balance-sheet side the centre donut shows. A donut can't carry
        # a negative slice, so assets and debts are separate datasets; an
        # Assets | Debts toggle swaps between them in one big donut rather than
        # cramming a second, too-small donut beside the legend. Per-refresh
        # segments + totals are cached so the toggle re-renders without a
        # recompute.
        self._side: str = "asset"
        self._side_segments: dict[str, list[DonutSegment]] = {"asset": [], "debt": []}
        self._side_total: dict[str, Decimal] = {
            "asset": Decimal("0.00"), "debt": Decimal("0.00"),
        }
        self._family_totals: list[_FamilyTotal] = []
        self._symbol: str = "£"

        # Net-worth-over-time view (ADR-121): a "Now | Over time" toggle swaps
        # the point-in-time donut/columns for a historical chart. The history is
        # heavier to compute, so it's built lazily on first view + when the
        # period / currency / closed-toggle change while it's showing.
        self._view: str = "now"
        self._history_period: str = "1y"
        # Granularity + custom range (ADR-135), in line with the other
        # over-time reports. "auto" picks a bucket from the span.
        self._history_granularity: str = "auto"
        self._history_dirty: bool = True

        # ── page header (ADR-119): title + the display-currency selector and
        # show-closed toggle in the action slot ──
        self._ccy_combo = QComboBox()
        self._ccy_combo.currentIndexChanged.connect(self._on_ccy_changed)
        self._show_closed_chk = QCheckBox("Show closed accounts")
        self._show_closed_chk.setToolTip(
            "Include closed (archived) accounts and their balances in the "
            "totals, donuts, and columns (ADR-069)."
        )
        self._show_closed_chk.toggled.connect(self._on_show_closed_toggled)
        self._populate_ccy_combo()

        # Now | Over time view toggle (ADR-121) — same pill control as the
        # Assets | Debts donut toggle.
        self._now_btn = QPushButton("Now")
        self._overtime_btn = QPushButton("Over time")
        for b in (self._now_btn, self._overtime_btn):
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            tokens.themed(
                b,
                "QPushButton { padding: 4px 14px; border: 1px solid {border_strong}; "
                "border-radius: 13px; background-color: {surface}; color: {heading}; "
                "font-size: 12px; }"
                "QPushButton:checked { background-color: {accent}; color: {surface}; "
                "border-color: {accent}; font-weight: bold; }"
                "QPushButton:hover:!checked { background-color: {surface_alt}; }",
            )
        self._now_btn.setChecked(True)
        view_group = QButtonGroup(self)
        view_group.setExclusive(True)
        view_group.addButton(self._now_btn)
        view_group.addButton(self._overtime_btn)
        self._now_btn.clicked.connect(lambda: self._set_view("now"))
        self._overtime_btn.clicked.connect(lambda: self._set_view("overtime"))

        top_bar = PageHeader(show_rule=True)
        top_bar.set_heading("Net Worth", "Assets, debts, and what you're worth")
        top_bar.add_leading(self._now_btn)
        top_bar.add_leading(self._overtime_btn)
        top_bar.add_action(QLabel("Display currency:"))
        top_bar.add_action(self._ccy_combo)
        top_bar.add_action(self._show_closed_chk)

        # ── missing-rate banner (hidden unless something can't convert) ──
        self._banner = QFrame()
        self._banner.setStyleSheet(
            "QFrame { background: #fef3c7; border: 1px solid #f59e0b; "
            "border-radius: 6px; }"
        )
        banner_layout = QHBoxLayout(self._banner)
        banner_layout.setContentsMargins(12, 8, 12, 8)
        self._banner_label = QLabel("")
        self._banner_label.setWordWrap(True)
        tokens.themed(self._banner_label, "color: {warning}; border: none;")
        banner_layout.addWidget(self._banner_label, 1)
        self._banner_btn = QPushButton("Set exchange rate…")
        self._banner_btn.clicked.connect(self._on_set_rate)
        banner_layout.addWidget(self._banner_btn, 0)
        self._banner.setVisible(False)

        # ── columns ──
        self._summary_panel, self._summary_total_lbl, \
            self._donut, self._legend_layout = self._build_summary_panel()
        # Outer-ring slice click → re-emit the account id for the register
        # window to open the Account Summary (ADR-083).
        self._donut.account_clicked.connect(self.account_activated)
        self._assets_panel, self._assets_total_lbl, \
            self._assets_tree = self._build_side_panel(is_asset=True)
        self._debts_panel, self._debts_total_lbl, \
            self._debts_tree = self._build_side_panel(is_asset=False)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._summary_panel)
        splitter.addWidget(self._assets_panel)
        splitter.addWidget(self._debts_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([500, 370, 370])

        # Page 0 — the point-in-time view (banner + donut/columns).
        now_page = QWidget()
        now_v = QVBoxLayout(now_page)
        now_v.setContentsMargins(0, 0, 0, 0)
        now_v.setSpacing(8)
        now_v.addWidget(self._banner)
        now_v.addWidget(splitter, stretch=1)

        # Page 1 — net worth over time (ADR-121).
        history_page = self._build_history_page()

        self._view_stack = QStackedWidget()
        self._view_stack.addWidget(now_page)        # index 0
        self._view_stack.addWidget(history_page)    # index 1

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(8)
        central_layout.addWidget(top_bar)
        central_layout.addWidget(self._view_stack, stretch=1)
        self.setCentralWidget(central)
        self._refresh()

    # ── builders ──

    def _populate_ccy_combo(self) -> None:
        """Fill the display-currency selector from the currencies in use,
        defaulting to the person's base currency (then GBP, then the first in
        use). Built once; selection drives the conversion target."""
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

    def _build_summary_panel(
        self,
    ) -> tuple[QWidget, QLabel, DonutChart, QVBoxLayout]:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(8)

        title = QLabel("Net Worth")
        tokens.themed(title, "color: {muted};")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 2)
        title.setFont(title_font)

        total_lbl = QLabel("£0.00")
        big = total_lbl.font()
        big.setPointSize(big.pointSize() + 16)
        big.setBold(True)
        total_lbl.setFont(big)

        # Assets | Debts segmented toggle (centred) — swaps which side the one
        # big donut shows. Matches the Income & Expense donut toggle (ADR-113).
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        toggle_row.setSpacing(6)
        toggle_row.addStretch(1)
        self._assets_btn = QPushButton("Assets")
        self._debts_btn = QPushButton("Debts")
        for b in (self._assets_btn, self._debts_btn):
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            tokens.themed(
                b,
                "QPushButton { padding: 4px 14px; border: 1px solid {border_strong}; "
                "border-radius: 13px; background-color: {surface}; color: {heading}; "
                "font-size: 12px; }"
                "QPushButton:checked { background-color: {accent}; color: {surface}; "
                "border-color: {accent}; font-weight: bold; }"
                "QPushButton:hover:!checked { background-color: {surface_alt}; }",
            )
            toggle_row.addWidget(b)
        toggle_row.addStretch(1)
        self._assets_btn.setChecked(True)
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self._assets_btn)
        group.addButton(self._debts_btn)
        self._assets_btn.clicked.connect(lambda: self._set_side("asset"))
        self._debts_btn.clicked.connect(lambda: self._set_side("debt"))

        # The one big donut — takes the available vertical space.
        donut = DonutChart()
        donut.setMinimumHeight(260)

        # Legend rows (the active side's family colour key) added by _refresh.
        legend = QVBoxLayout()
        legend.setContentsMargins(0, 0, 0, 0)
        legend.setSpacing(4)
        legend_holder = QWidget()
        legend_holder.setLayout(legend)
        self._legend_holder = legend_holder

        layout.addWidget(title)
        layout.addWidget(total_lbl)
        layout.addSpacing(4)
        layout.addLayout(toggle_row)
        layout.addSpacing(4)
        layout.addWidget(donut, stretch=1)
        layout.addSpacing(4)
        layout.addWidget(legend_holder)
        return (panel, total_lbl, donut, legend)

    def _build_side_panel(self, *, is_asset: bool) -> tuple[QWidget, QLabel, QTreeWidget]:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(8)

        # Header row: title (left) + total (right).
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Assets" if is_asset else "Debts")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 4)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet(
            f"color: {(_ASSET_COLOR if is_asset else _DEBT_COLOR).name()};"
        )
        total_lbl = QLabel("£0.00")
        total_font = total_lbl.font()
        total_font.setPointSize(total_font.pointSize() + 4)
        total_font.setBold(True)
        total_lbl.setFont(total_font)
        total_lbl.setStyleSheet(
            f"color: {(_ASSET_COLOR if is_asset else _DEBT_COLOR).name()};"
        )
        total_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header_row.addWidget(title)
        header_row.addStretch(1)
        header_row.addWidget(total_lbl)

        subhead = QLabel("WHAT I OWN" if is_asset else "WHAT I OWE")
        tokens.themed(subhead, "color: {muted}; letter-spacing: 1px;")

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)

        tree = QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderHidden(True)
        tree.setRootIsDecorated(True)
        tree.setIndentation(14)
        tree.setUniformRowHeights(False)
        tree.setSelectionMode(QAbstractItemView.NoSelection)
        tree.setFocusPolicy(Qt.NoFocus)
        header = tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)

        add_btn = QPushButton("+ Asset" if is_asset else "+ Debt")
        add_btn.setStyleSheet(
            f"padding: 10px; font-weight: bold; "
            f"color: {(_ASSET_COLOR if is_asset else _DEBT_COLOR).name()};"
        )
        add_btn.clicked.connect(
            self._on_add_asset if is_asset else self._on_add_debt
        )

        layout.addLayout(header_row)
        layout.addWidget(subhead)
        layout.addWidget(sep)
        layout.addWidget(tree, stretch=1)
        layout.addWidget(add_btn)
        return panel, total_lbl, tree

    # ── currency conversion ──

    def _convert_all(
        self, accounts: list[AccountSummary], native: dict[int, Decimal],
    ) -> None:
        """Convert every account's native value to the display currency, into
        ``self._converted`` (None when no rate is on file). Non-zero
        unconvertable accounts are collected into ``self._missing`` for the
        banner. Mirrors ADR-046's _conv, but EXCLUDES rather than 1:1-folds a
        missing rate — a par-add is exactly the bug this fixes (ADR-055)."""
        self._converted = {}
        self._native = dict(native)        # for native-amount tooltips
        self._missing = []
        self._fallback_used = False
        today = date.today().isoformat()
        for a in accounts:
            val = native.get(a.id, Decimal("0.00"))
            if a.currency == self._display_ccy:
                self._converted[a.id] = val
                continue
            converted, fallback = self._repo.convert_amount(
                val, from_ccy=a.currency, to_ccy=self._display_ccy,
                on_date=today,
            )
            if converted is None:
                self._converted[a.id] = None
                if val != 0:
                    self._missing.append((a, val))
            else:
                self._converted[a.id] = converted
                self._fallback_used = self._fallback_used or fallback

    def _sum_converted(self, members: list[AccountSummary], *, negate: bool) -> Decimal:
        """Sum the display-currency values of ``members``, skipping any whose
        rate is missing. ``negate`` flips the sign for liabilities (stored
        negative, shown as positive owed)."""
        total = Decimal("0.00")
        for m in members:
            v = self._converted.get(m.id)
            if v is None:
                continue
            total += (-v if negate else v)
        return total

    def _update_banner(self) -> None:
        """Show / hide the excluded-accounts banner. Groups the missing
        accounts by currency and states each native sum, so the user knows
        exactly what's left out and what rate to add."""
        if not self._missing:
            self._banner.setVisible(False)
            return
        by_ccy: dict[str, tuple[int, Decimal]] = {}
        for acct, val in self._missing:
            n, s = by_ccy.get(acct.currency, (0, Decimal("0.00")))
            by_ccy[acct.currency] = (n + 1, s + abs(val))
        parts = []
        for ccy, (n, s) in sorted(by_ccy.items()):
            parts.append(
                f"{n} {ccy} account{'s' if n != 1 else ''} "
                f"({_symbol(ccy)}{s:,.2f})"
            )
        self._banner_label.setText(
            "Excluded from the totals — no exchange rate to "
            f"{self._display_ccy}: " + "; ".join(parts)
            + ".  Add a rate to include them."
        )
        self._banner.setVisible(True)

    # ── data + render ──

    def _refresh(self) -> None:
        accounts = self._repo.list_accounts(include_closed=self._include_closed)
        # Market value, not cash: investment accounts contribute
        # cash + Σ(shares × latest price); priced via security_price, falling
        # back to cash when unpriced (ADR-044, closing the ADR-019 follow-up).
        native = self._repo.compute_account_values(
            include_closed=self._include_closed
        )

        # Convert to the display currency before any summation (ADR-055).
        self._convert_all(accounts, native)
        self._update_banner()

        # Group by family.
        by_family: dict[str, list[AccountSummary]] = {}
        for a in accounts:
            by_family.setdefault(a.family, []).append(a)

        # Build a FamilyTotal for every family we know how to display, in
        # the configured order. Families we have no view-row for fall
        # through silently — once a new family ships, add a row above.
        family_totals: list[_FamilyTotal] = []
        for fam, label, color, kind in _FAMILY_VIEW:
            members = sorted(
                by_family.get(fam, []),
                key=lambda a: a.name.lower(),
            )
            # Liability balances are stored negative; show debt positive.
            total = self._sum_converted(members, negate=(kind == "debt"))
            family_totals.append(_FamilyTotal(
                family=fam, label=label, color=color, kind=kind,
                accounts=members, total=total,
            ))

        # Summary numbers.
        asset_total = sum(
            (ft.total for ft in family_totals if ft.kind == "asset"),
            start=Decimal("0.00"),
        )
        debt_total = sum(
            (ft.total for ft in family_totals if ft.kind == "debt"),
            start=Decimal("0.00"),
        )
        net_worth = asset_total - debt_total

        # Net worth label (signed).
        self._summary_total_lbl.setText(self._format_signed(net_worth))
        self._summary_total_lbl.setStyleSheet(
            "color: " + (_ASSET_COLOR.name() if net_worth >= 0 else _DEBT_COLOR.name()) + ";"
        )

        # Type-level totals power both the donuts and the Assets / Debts
        # columns (finer-grained than family).
        type_totals = self._compute_type_totals(by_family)

        # Donuts (ADR-067): inner ring = account type, outer ring = the
        # individual accounts. Assets and debts get separate donuts because a
        # donut can't carry a negative slice; debts are over positive
        # magnitudes (the stored-negative balances are flipped in
        # _compute_type_totals / _donut_segments).
        self._symbol = _symbol(self._display_ccy) or "£"
        self._side_segments = {
            "asset": self._donut_segments(type_totals, kind="asset"),
            "debt":  self._donut_segments(type_totals, kind="debt"),
        }
        self._side_total = {"asset": asset_total, "debt": debt_total}
        self._family_totals = family_totals

        # Reserve a constant legend height = the side with the most families,
        # so the donut above (stretch=1) keeps the same size when the toggle
        # swaps to a side with fewer rows (otherwise the donut grows/shrinks).
        asset_rows = sum(
            1 for ft in family_totals if ft.kind == "asset" and ft.total > 0
        )
        debt_rows = sum(
            1 for ft in family_totals if ft.kind == "debt" and ft.total > 0
        )
        self._reserve_legend_height(max(asset_rows, debt_rows))

        # The Debts side is only offered when there are debts; an all-asset
        # file falls back to Assets and disables the Debts toggle.
        has_debts = bool(self._side_segments["debt"])
        self._debts_btn.setEnabled(has_debts)
        if self._side == "debt" and not has_debts:
            self._side = "asset"
            self._assets_btn.setChecked(True)
        self._render_active_side()

        # Assets column header total + tree.
        self._assets_total_lbl.setText(self._format(asset_total))
        self._fill_tree(self._assets_tree, type_totals, kind="asset")

        # Debts column header total + tree.
        self._debts_total_lbl.setText(self._format(debt_total))
        self._fill_tree(self._debts_tree, type_totals, kind="debt")

        # The underlying data may have changed — the history view (if showing)
        # rebuilds now; otherwise it's marked stale for the next time it opens.
        self._history_dirty = True
        if self._view == "overtime":
            self._refresh_history()

    # ── net worth over time (ADR-121) ──

    def _build_history_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(20, 8, 20, 16)
        v.setSpacing(8)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        controls.addWidget(QLabel("Period:"))
        self._history_period_combo = QComboBox()
        for label, key in [
            ("Last 12 months", "1y"),
            ("Last 3 years", "3y"),
            ("Last 5 years", "5y"),
            ("All time", "all"),
            ("Custom…", "custom"),
        ]:
            self._history_period_combo.addItem(label, key)
        self._history_period_combo.currentIndexChanged.connect(
            self._on_history_period_changed
        )
        controls.addWidget(self._history_period_combo)

        # Custom range pickers (ADR-135) — shown only for the Custom preset.
        today = date.today()
        self._history_from_label = QLabel("From:")
        self._history_from = make_date_edit(
            QDate(max(1, today.year - 1), today.month, today.day)
        )
        self._history_to_label = QLabel("To:")
        self._history_to = make_date_edit(QDate(today.year, today.month, today.day))
        for w in (self._history_from, self._history_to):
            w.dateChanged.connect(self._on_history_custom_changed)
        controls.addWidget(self._history_from_label)
        controls.addWidget(self._history_from)
        controls.addWidget(self._history_to_label)
        controls.addWidget(self._history_to)

        controls.addSpacing(12)
        controls.addWidget(QLabel("Granularity:"))
        self._history_gran_combo = QComboBox()
        for label, value in GRANULARITY_OPTIONS:
            self._history_gran_combo.addItem(label, value)
        self._set_combo_data(self._history_gran_combo, self._history_granularity)
        self._history_gran_combo.currentIndexChanged.connect(
            self._on_history_granularity_changed
        )
        controls.addWidget(self._history_gran_combo)
        controls.addStretch(1)

        self._history_chart = NetWorthHistoryChart()
        v.addLayout(controls)
        v.addWidget(self._history_chart, stretch=1)
        self._sync_history_custom_visibility()
        return page

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.blockSignals(True)
                combo.setCurrentIndex(i)
                combo.blockSignals(False)
                return

    def _sync_history_custom_visibility(self) -> None:
        show = self._history_period == "custom"
        for w in (self._history_from_label, self._history_from,
                  self._history_to_label, self._history_to):
            w.setVisible(show)

    def _set_view(self, view: str) -> None:
        if view == self._view:
            return
        self._view = view
        self._now_btn.setChecked(view == "now")
        self._overtime_btn.setChecked(view == "overtime")
        self._view_stack.setCurrentIndex(0 if view == "now" else 1)
        if view == "overtime" and self._history_dirty:
            self._refresh_history()

    def _on_history_period_changed(self, _idx: int) -> None:
        key = self._history_period_combo.currentData()
        if key:
            self._history_period = key
            self._sync_history_custom_visibility()
            self._refresh_history()

    def _on_history_granularity_changed(self, _idx: int) -> None:
        value = self._history_gran_combo.currentData()
        if value:
            self._history_granularity = value
            self._refresh_history()

    def _on_history_custom_changed(self, *_a) -> None:
        # Only a real edit while the Custom preset is active recomputes; the
        # dateChanged that fires during setup / while hidden is ignored.
        if self._history_period == "custom":
            self._refresh_history()

    def _history_bounds(self) -> tuple[date, date]:
        """(start, end) for the chosen preset. 'all' starts at the earliest
        transaction (ADR-121); 'custom' reads the pickers (swapped if
        from > to); the rolling presets are a whole-year day-delta back."""
        today = date.today()
        key = self._history_period
        if key == "custom":
            start = self._history_from.date().toPython()
            end = self._history_to.date().toPython()
            if start > end:
                start, end = end, start
            return start, end
        if key == "all":
            earliest = self._repo.earliest_posted_date()
            start = date.fromisoformat(earliest) if earliest else today.replace(
                year=max(1, today.year - 5)
            )
            return start, today
        years = {"1y": 1, "3y": 3, "5y": 5}.get(key, 1)
        return today - timedelta(days=365 * years), today

    def _history_sample_dates(self) -> list:
        """Sample dates for the chosen period + granularity (ADR-135) — the
        period-ends across the range, with 'auto' resolved from the span."""
        start, end = self._history_bounds()
        if start > end:
            start = end
        gran = resolve_history_granularity(start, end, self._history_granularity)
        return period_end_samples(start, end, gran)

    def _refresh_history(self) -> None:
        """Recompute + render the net-worth-over-time series for the current
        display currency / period / closed scope."""
        family_kinds = {fam: kind for fam, _l, _c, kind in _FAMILY_VIEW}
        samples = self._history_sample_dates()
        hist = gather_net_worth_history(
            self._repo,
            sample_dates=samples,
            display_ccy=self._display_ccy,
            family_kinds=family_kinds,
            include_closed=self._include_closed,
        )
        self._history_dirty = False
        if len(hist.points) < 2:
            self._history_chart.show_empty("Not enough history to chart yet.")
            return
        asset_families = [
            (fam, label, color)
            for fam, label, color, kind in _FAMILY_VIEW if kind == "asset"
        ]
        debt_families = [
            (fam, label, color)
            for fam, label, color, kind in _FAMILY_VIEW if kind == "debt"
        ]
        self._history_chart.render(
            points=hist.points,
            asset_families=asset_families,
            debt_families=debt_families,
            symbol=_symbol(self._display_ccy) or "£",
            any_excluded=hist.excluded_any,
        )

    def _render_active_side(self) -> None:
        """Draw the active balance-sheet side (assets or debts) into the one
        big donut + the legend, from the cached per-refresh data — the toggle
        re-renders without recomputing anything."""
        side = self._side
        segs = self._side_segments.get(side, [])
        label = "Assets" if side == "asset" else "Debts"
        if not segs:
            self._donut.show_empty(f"No {label.lower()}")
        else:
            self._donut.set_data(
                segments=segs, center_label=label,
                center_sub=self._format(self._side_total[side]),
                symbol=self._symbol,
            )
        self._rebuild_legend(side)

    def _set_side(self, side: str) -> None:
        if side == self._side:
            return
        self._side = side
        # Keep the toggle in sync even when called programmatically.
        self._assets_btn.setChecked(side == "asset")
        self._debts_btn.setChecked(side == "debt")
        self._render_active_side()

    def _compute_type_totals(
        self,
        accounts_by_family: dict[str, list[AccountSummary]],
    ) -> list[_TypeTotal]:
        """Roll up accounts by account.type — finer-grained than family —
        and pair each type with its family colour and kind. Totals are in the
        display currency (ADR-055), skipping unconvertable accounts."""
        family_color = {fam: color for fam, _, color, _ in _FAMILY_VIEW}
        family_kind = {fam: kind for fam, _, _, kind in _FAMILY_VIEW}

        # Pre-bucket accounts by storage type for cheap lookup.
        accounts_by_type: dict[str, list[AccountSummary]] = {}
        for fam_accounts in accounts_by_family.values():
            for a in fam_accounts:
                accounts_by_type.setdefault(a.type, []).append(a)

        result: list[_TypeTotal] = []
        for spec in ACCOUNT_TYPES:
            members = sorted(
                accounts_by_type.get(spec.storage, []),
                key=lambda a: a.name.lower(),
            )
            if not members:
                continue
            kind = family_kind.get(spec.family, "asset")
            total = self._sum_converted(members, negate=(kind == "debt"))
            result.append(_TypeTotal(
                type_storage=spec.storage,
                type_label=spec.label,
                family=spec.family,
                color=family_color.get(spec.family, QColor("#94a3b8")),
                kind=kind,
                accounts=members,
                total=total,
            ))
        return result

    def _donut_segments(
        self, type_totals: list[_TypeTotal], *, kind: str,
    ) -> list[DonutSegment]:
        """Build the two-ring donut data for one balance-sheet side (ADR-067):
        one :class:`DonutSegment` per account type (inner ring), each carrying
        its accounts as :class:`DonutChild` outer slices. Values are in the
        display currency; unconvertable accounts (no rate) are skipped — they
        already show in the missing-rate banner. Debt balances (stored
        negative) are flipped to positive magnitudes so they form a donut."""
        segments: list[DonutSegment] = []
        for tt in type_totals:
            if tt.kind != kind or tt.total <= 0:
                continue
            members: list[tuple[int, str, float]] = []
            for acct in tt.accounts:
                conv = self._converted.get(acct.id)
                if conv is None:
                    continue
                val = float(-conv if kind == "debt" else conv)
                if val <= 0:
                    continue
                members.append((acct.id, acct.name, val))
            n = len(members)
            children = tuple(
                DonutChild(
                    label=name, value=val, color=self._shade(tt.color, i, n),
                    account_id=acct_id,
                )
                for i, (acct_id, name, val) in enumerate(members)
            )
            segments.append(DonutSegment(
                label=tt.type_label, value=float(tt.total),
                color=tt.color, children=children,
            ))
        return segments

    @staticmethod
    def _shade(base: QColor, index: int, count: int) -> QColor:
        """Progressively lighter tints of a type's colour so the individual
        accounts in the outer ring are distinguishable while staying clearly
        part of the same type."""
        if count <= 1:
            return base.lighter(118)
        factor = 112 + int(48 * index / (count - 1))
        return base.lighter(factor)

    def _rebuild_legend(self, kind: str) -> None:
        """Render the colour key for just the active side's families — the
        Assets | Debts toggle already names the side, so no section heading is
        needed (it would only repeat the toggle)."""
        # Drop every previous legend row.
        while self._legend_layout.count():
            item = self._legend_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for ft in self._family_totals:
            if ft.kind == kind and ft.total > 0:
                self._legend_layout.addWidget(self._legend_row(ft))

    def _reserve_legend_height(self, rows: int) -> None:
        """Pin the legend area to the pixel height of ``rows`` legend rows, so
        the stretch-driven donut above keeps a constant size across the
        Assets | Debts toggle (the smaller side just leaves whitespace below
        its rows instead of letting the donut grow)."""
        if rows <= 0 or not self._family_totals:
            self._legend_holder.setMinimumHeight(0)
            return
        probe = self._legend_row(self._family_totals[0])
        row_h = probe.sizeHint().height()
        probe.deleteLater()
        spacing = max(0, self._legend_layout.spacing())
        self._legend_holder.setMinimumHeight(rows * row_h + (rows - 1) * spacing)

    def _legend_row(self, ft: _FamilyTotal) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)

        swatch = QLabel()
        swatch.setFixedSize(14, 14)
        swatch.setStyleSheet(
            f"background-color: {ft.color.name()}; border-radius: 3px;"
        )

        label = QLabel(ft.label)
        tokens.themed(label, "color: {text};")

        amount = QLabel(self._format(ft.total))
        tokens.themed(amount, "color: {text}; font-weight: bold;")
        amount.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        layout.addWidget(swatch)
        layout.addWidget(label)
        layout.addStretch(1)
        layout.addWidget(amount)
        return row

    def _fill_tree(
        self,
        tree: QTreeWidget,
        type_totals: list[_TypeTotal],
        *,
        kind: str,
    ) -> None:
        tree.clear()
        bold = QFont()
        bold.setBold(True)
        for tt in type_totals:
            if tt.kind != kind:
                continue
            count = len(tt.accounts)
            group_item = QTreeWidgetItem([
                f"  {tt.type_label}",
                self._format(tt.total),
            ])
            group_item.setFont(0, bold)
            group_item.setFont(1, bold)
            group_item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            group_item.setForeground(0, QBrush(tt.color))
            group_item.setToolTip(
                0, f"{count} account{'s' if count != 1 else ''}",
            )
            tree.addTopLevelItem(group_item)

            for acct in tt.accounts:
                conv = self._converted.get(acct.id)
                native = self._native.get(acct.id, Decimal("0.00"))
                # Mark closed accounts when the toggle has surfaced them
                # (ADR-069) so the figure isn't mistaken for a live balance.
                name = acct.name + (" (closed)" if acct.is_closed else "")
                if conv is None:
                    # No rate — show the native value + a flag rather than a
                    # fabricated converted figure.
                    shown_native = -native if kind == "debt" else native
                    child = QTreeWidgetItem([
                        name,
                        f"{_symbol(acct.currency)}{shown_native:,.2f} (no rate)",
                    ])
                    child.setForeground(1, QBrush(QColor(tokens.c("warning"))))
                    child.setToolTip(
                        1, f"No {acct.currency}→{self._display_ccy} rate on file.",
                    )
                else:
                    shown = -conv if kind == "debt" else conv
                    child = QTreeWidgetItem([name, self._format(shown)])
                    if acct.currency != self._display_ccy:
                        nshown = -native if kind == "debt" else native
                        child.setToolTip(
                            1, f"Native: {_symbol(acct.currency)}{nshown:,.2f} "
                               f"{acct.currency}",
                        )
                child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                group_item.addChild(child)
            group_item.setExpanded(True)

    # ── actions ──

    def _on_ccy_changed(self, _idx: int) -> None:
        data = self._ccy_combo.currentData()
        if data:
            self._display_ccy = data
            self._refresh()

    def _on_show_closed_toggled(self, checked: bool) -> None:
        self._include_closed = checked
        self._refresh()

    def _on_set_rate(self) -> None:
        """Open the Currencies dialog so the user can add (or fetch) the
        missing rate, then re-render with whatever they entered."""
        CurrenciesDialog(self._repo, self).exec()
        self._refresh()

    def _on_add_asset(self) -> None:
        self._open_account_dialog()

    def _on_add_debt(self) -> None:
        self._open_account_dialog()

    def _open_account_dialog(self) -> None:
        dialog = AccountDialog(existing=None, parent=self)
        if dialog.exec() != AccountDialog.Accepted:
            return
        values = dialog.values()
        if values is None or values.type_key is None:
            return
        try:
            self._repo.create_account(
                name=values.name,
                type_key=values.type_key,
                currency=values.currency,
                opening_balance=values.opening_balance,
                credit_limit=values.credit_limit,
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Could not create account",
                f"The account was not created:\n\n{e}",
            )
            return
        # A new currency may have appeared — keep the selector in step.
        self._populate_ccy_combo()
        self._refresh()
        # Tell the register window to refresh its sidebar too.
        owner = self.parent()
        reload_sidebar = getattr(owner, "_reload_sidebar", None)
        if callable(reload_sidebar):
            reload_sidebar(None)

    # ── formatting ──

    def _format(self, amount: Decimal) -> str:
        sym = _symbol(self._display_ccy)
        body = f"{amount:,.2f}"
        return f"{sym}{body}" if sym else f"{body} {self._display_ccy}"

    def _format_signed(self, amount: Decimal) -> str:
        sym = _symbol(self._display_ccy)
        body = f"{abs(amount):,.2f}"
        sign = "-" if amount < 0 else ""
        if sym:
            return f"{sign}{sym}{body}"
        return f"{sign}{body} {self._display_ccy}"
