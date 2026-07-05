"""Home / dashboard view (ADR-075, Arc F; themed per ADR-076).

Presentation only: renders the Qt-free ``HomeData`` (from
``home_dashboard.gather_home_data``) into a scrollable grid of cards and emits
navigation signals the register window wires to its existing handlers. Each
card hides when it has nothing to show. ``refresh()`` re-gathers and rebuilds.

All colours come from ``ui.tokens`` (via ``themed``/object-name QSS) so the
screen follows the light/dark theme live.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.home_dashboard import gather_home_data
from mfl_desktop.ui import tokens

_CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥"}


def _sym(ccy: str) -> str:
    return _CURRENCY_SYMBOLS.get((ccy or "").upper(), "")


def _fmt(amount: Decimal, ccy: str, *, decimals: int = 2, signed: bool = False) -> str:
    sym = _sym(ccy)
    neg = amount < 0
    body = f"{abs(amount):,.{decimals}f}"
    sign = "−" if neg else ("+" if signed and amount > 0 else "")
    code = "" if sym else f" {ccy}"
    return f"{sign}{sym}{body}{code}"


class _Card(QFrame):
    """A titled card. Optionally clickable (whole-card navigation). The frame
    background/border come from the global `QFrame#homeCard` QSS (ADR-076)."""
    clicked = Signal()

    def __init__(self, title: str, parent=None, action: str = "") -> None:
        super().__init__(parent)
        self.setObjectName("homeCard")
        self._clickable = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        # Header row: muted-caps title on the left, an optional accent "action →"
        # link on the right (ADR-119) — the MRL affordance for a clickable card.
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        header = QLabel(title)
        tokens.themed(
            header,
            "font-size: 11px; font-weight: 600; color: {muted}; "
            "letter-spacing: 0.04em;",
        )
        header_row.addWidget(header, 1)
        if action:
            link = QLabel(action)
            tokens.themed(link, "font-size: 11px; font-weight: 600; color: {accent};")
            header_row.addWidget(link, 0)
        lay.addLayout(header_row)
        self._body = QVBoxLayout()
        self._body.setSpacing(4)
        lay.addLayout(self._body)
        lay.addStretch(1)

    def body(self) -> QVBoxLayout:
        return self._body

    def make_clickable(self) -> None:
        self._clickable = True
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e) -> None:  # noqa: N802 (Qt override)
        # Run base handling first, then emit — the clicked slot can navigate /
        # refresh Home and destroy this very card, so we must not touch ``self``
        # (e.g. super().mousePressEvent) afterwards (use-after-free crash).
        super().mousePressEvent(e)
        if self._clickable:
            self.clicked.emit()


class _Row(QFrame):
    """A two-column (left/right) row, optionally clickable for navigation.
    ``right_token`` names a colour token for the right-hand value."""
    clicked = Signal()

    def __init__(self, left: str, right: str, *, right_token: str = "",
                 clickable: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._clickable = clickable
        if clickable:
            self.setCursor(Qt.PointingHandCursor)
            tokens.themed(
                self, ":hover { background: {surface_alt}; border-radius: 6px; }",
            )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 3, 2, 3)
        lay.setSpacing(8)
        left_lbl = QLabel(left)
        left_lbl.setTextFormat(Qt.PlainText)
        right_lbl = QLabel(right)
        if right_token:
            tokens.themed(right_lbl, "color: {%s};" % right_token)
        lay.addWidget(left_lbl, stretch=1)
        lay.addWidget(right_lbl, stretch=0)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        # super() before emit: the clicked slot may delete this row (Home
        # rebuild on navigation), so never touch self after emitting.
        super().mousePressEvent(e)
        if self._clickable:
            self.clicked.emit()


class _AccordionHeader(QFrame):
    """A clickable family header (chevron + label + subtotal) that toggles its
    account rows. Starts collapsed so the Accounts card stays concise."""
    toggled = Signal(bool)

    def __init__(self, title: str, value: str, parent=None) -> None:
        super().__init__(parent)
        self._expanded = False
        self.setCursor(Qt.PointingHandCursor)
        tokens.themed(
            self, ":hover { background: {surface_alt}; border-radius: 6px; }",
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 4, 2, 4)
        lay.setSpacing(6)
        self._chev = QLabel("▸")
        tokens.themed(self._chev, "color: {muted};")
        self._chev.setFixedWidth(12)
        title_lbl = QLabel(title)
        tokens.themed(title_lbl, "font-weight: 600; color: {heading};")
        value_lbl = QLabel(value)
        tokens.themed(value_lbl, "font-weight: 600; color: {heading};")
        lay.addWidget(self._chev)
        lay.addWidget(title_lbl, stretch=1)
        lay.addWidget(value_lbl)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        self._expanded = not self._expanded
        self._chev.setText("▾" if self._expanded else "▸")
        # super() before emit so we never call into a freed object if the
        # toggle slot ever rebuilds this header.
        super().mousePressEvent(e)
        self.toggled.emit(self._expanded)


class HomeView(QWidget):
    net_worth_requested = Signal()
    budget_requested = Signal()
    schedules_requested = Signal()
    payee_report_requested = Signal()
    spending_report_requested = Signal()
    account_requested = Signal(str)        # account iri

    def __init__(self, repo, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        tokens.themed(self._scroll, "QScrollArea { background: {canvas}; }")
        outer.addWidget(self._scroll)
        self._container: Optional[QWidget] = None

    def set_repo(self, repo) -> None:
        """Point the dashboard at a different file (File ▸ Open swaps the live
        repo — ADR-092). Without this the view keeps reading the old, now-closed
        repo and shows stale data until restart. Caller refreshes after."""
        self._repo = repo

    # ── build ──

    def refresh(self) -> None:
        if not self._repo.is_open():
            return
        try:
            data = gather_home_data(self._repo, date.today())
        except Exception:
            return
        container = QWidget()
        tokens.themed(container, "background: {canvas};")
        root = QVBoxLayout(container)
        root.setContentsMargins(16, 4, 16, 16)
        root.setSpacing(16)

        # ADR-119: the net-worth hero spans the full width above the grid.
        root.addWidget(self._hero_card(data))

        # Two independently-packed columns (greedy-balanced by an approximate
        # per-card height weight) so a tall card never leaves the other column
        # with a big blank gap.
        left = QVBoxLayout()
        right = QVBoxLayout()
        left.setSpacing(16)
        right.setSpacing(16)
        left_w = right_w = 0
        for card in self._build_cards(data):
            if card is None:
                continue
            weight = getattr(card, "_weight", 4)
            if left_w <= right_w:
                left.addWidget(card)
                left_w += weight
            else:
                right.addWidget(card)
                right_w += weight
        left.addStretch(1)
        right.addStretch(1)

        left_wrap = QWidget()
        left_wrap.setLayout(left)
        right_wrap = QWidget()
        right_wrap.setLayout(right)
        grid = QHBoxLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(16)
        grid.addWidget(left_wrap, 1)
        grid.addWidget(right_wrap, 1)
        grid_wrap = QWidget()
        grid_wrap.setLayout(grid)
        root.addWidget(grid_wrap)

        self._scroll.setWidget(container)
        self._container = container

    def _build_cards(self, data) -> list:
        return [
            self._budget_card(data),
            self._accounts_card(data),
            self._bills_card(data),
            self._recent_card(data),
            self._top_payees_card(data),
            self._top_categories_card(data),
            self._investments_card(data),
        ]

    # ── individual cards ──

    def _hero_card(self, data) -> _Card:
        """The full-width net-worth hero (ADR-119) — big number, accent left
        edge (via the homeHeroCard object name), with a short summary line."""
        card = _Card("NET WORTH", action="Net worth →")
        card.setObjectName("homeHeroCard")
        big = QLabel(_fmt(data.net_worth, data.display_ccy, decimals=0))
        tokens.themed(big, "font-size: 40px; font-weight: 700; color: {text};")
        card.body().addWidget(big)
        n_accts = sum(len(g.accounts) for g in data.account_groups)
        if n_accts:
            sub = QLabel(
                f"across {n_accts} account{'s' if n_accts != 1 else ''}"
            )
            tokens.themed(sub, "color: {muted}; font-size: 12px;")
            card.body().addWidget(sub)
        if data.net_worth_excluded:
            note = QLabel(
                f"{data.net_worth_excluded} account"
                f"{'s' if data.net_worth_excluded != 1 else ''} excluded "
                f"(no exchange rate)"
            )
            tokens.themed(note, "color: {warning}; font-size: 11px;")
            card.body().addWidget(note)
        card.make_clickable()
        card.clicked.connect(self.net_worth_requested)
        return card

    def _budget_card(self, data) -> Optional[_Card]:
        b = data.budget
        if b is None:
            return None
        card = _Card(f"BUDGET · {b.month_label.upper()}", action="Open budget →")
        card._weight = 4
        planned = float(b.planned)
        spent = float(b.spent)
        rollover = float(getattr(b, "rollover", 0) or 0)
        # `planned` is THIS month's budget (allocation), not the rollover-inflated
        # `available` — so the headline reads as a monthly figure (ADR-136).
        if planned > 0:
            line = QLabel(
                f"{_fmt(b.spent, b.currency)} of "
                f"{_fmt(b.planned, b.currency)} budgeted this month"
            )
        else:
            line = QLabel(f"{_fmt(b.spent, b.currency)} spent this month")
        tokens.themed(line, "font-size: 15px; color: {text};")
        card.body().addWidget(line)
        bar = QProgressBar()
        # Bar tracks spend against this month's plan; when the plan is £0 (an
        # envelope funded purely by rollover) fall back to the rollover cushion
        # so the bar is still meaningful.
        denom = planned if planned > 0 else rollover
        pct = int(min(100, round(spent / denom * 100))) if denom > 0 else 0
        bar.setRange(0, 100)
        bar.setValue(pct)
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        over = planned > 0 and spent > planned
        chunk = "negative" if over else "accent"
        tokens.themed(
            bar,
            "QProgressBar { background: {border}; border: none; border-radius: 4px; } "
            "QProgressBar::chunk { background: {%s}; border-radius: 4px; }" % chunk,
        )
        card.body().addWidget(bar)
        if over:
            note = QLabel(
                f"Over this month's plan by "
                f"{_fmt(b.spent - b.planned, b.currency)}"
            )
            tokens.themed(note, "color: {negative}; font-size: 11px;")
            card.body().addWidget(note)
        if rollover > 0:
            roll = QLabel(f"+{_fmt(b.rollover, b.currency)} rolled over available")
            tokens.themed(roll, "color: {muted_strong}; font-size: 11px;")
            card.body().addWidget(roll)
        card.make_clickable()
        card.clicked.connect(self.budget_requested)
        return card

    def _accounts_card(self, data) -> _Card:
        card = _Card("ACCOUNTS")
        card._weight = len(data.account_groups) + 1
        if not data.account_groups:
            card.body().addWidget(_muted("No accounts yet."))
            return card
        for g in data.account_groups:
            header = _AccordionHeader(g.label, _fmt(g.subtotal, data.display_ccy))
            card.body().addWidget(header)
            child = QWidget()
            child_lay = QVBoxLayout(child)
            child_lay.setContentsMargins(0, 0, 0, 0)
            child_lay.setSpacing(0)
            for a in g.accounts:
                val = (
                    _fmt(a.value, data.display_ccy) if a.value is not None
                    else f"— ({a.currency})"
                )
                row = _Row("      " + a.name, val, clickable=True)
                row.clicked.connect(
                    lambda iri=a.iri: self.account_requested.emit(iri)
                )
                child_lay.addWidget(row)
            child.setVisible(False)
            header.toggled.connect(child.setVisible)
            card.body().addWidget(child)
        return card

    def _bills_card(self, data) -> _Card:
        title = "UPCOMING BILLS"
        if data.bills_overdue:
            title += f"  ·  {data.bills_overdue} OVERDUE"
        card = _Card(title, action="Schedules →")
        card._weight = len(data.bills) + 1
        if not data.bills:
            card.body().addWidget(_muted("Nothing scheduled."))
        else:
            for b in data.bills:
                when = (
                    "overdue" if b.overdue
                    else ("due today" if b.days_until == 0
                          else f"in {b.days_until} day{'s' if b.days_until != 1 else ''}")
                )
                row = _Row(
                    f"{b.label}  ·  {when}",
                    _fmt(b.amount, data.display_ccy),
                    right_token="negative" if b.overdue else "",
                )
                card.body().addWidget(row)
        card.make_clickable()
        card.clicked.connect(self.schedules_requested)
        return card

    def _recent_card(self, data) -> _Card:
        card = _Card("RECENT ACTIVITY")
        card._weight = len(data.recent) + 1
        if not data.recent:
            card.body().addWidget(_muted("No transactions yet."))
            return card
        for t in data.recent:
            label = f"{t.posted_date}  ·  {t.payee or t.category or t.account_name}"
            row = _Row(
                label, _fmt(t.amount, data.display_ccy, signed=True),
                right_token="positive" if t.amount > 0 else "",
                clickable=bool(t.account_iri),
            )
            if t.account_iri:
                row.clicked.connect(
                    lambda iri=t.account_iri: self.account_requested.emit(iri)
                )
            card.body().addWidget(row)
        return card

    def _top_payees_card(self, data) -> _Card:
        card = _Card("TOP PAYEES · THIS MONTH", action="Payees →")
        card._weight = len(data.top_payees) + 1
        if not data.top_payees:
            card.body().addWidget(_muted("No spending yet this month."))
        else:
            for p in data.top_payees:
                card.body().addWidget(
                    _Row(p.label, _fmt(p.amount, data.display_ccy))
                )
        card.make_clickable()
        card.clicked.connect(self.payee_report_requested)
        return card

    def _top_categories_card(self, data) -> _Card:
        card = _Card("TOP CATEGORIES · THIS MONTH", action="Spending →")
        card._weight = len(data.top_categories) + 1
        if not data.top_categories:
            card.body().addWidget(_muted("No spending yet this month."))
        else:
            for ct in data.top_categories:
                card.body().addWidget(
                    _Row(ct.label, _fmt(ct.amount, data.display_ccy))
                )
        card.make_clickable()
        card.clicked.connect(self.spending_report_requested)
        return card

    def _investments_card(self, data) -> Optional[_Card]:
        if not data.invest_gains and not data.invest_losses:
            return None
        card = _Card("INVESTMENT PERFORMANCE · UNREALISED")
        card._weight = len(data.invest_gains) + len(data.invest_losses) + 2
        if data.invest_gains:
            card.body().addWidget(_section_label("Top gains"))
            for h in data.invest_gains:
                card.body().addWidget(_perf_row(h, data.display_ccy, "positive"))
        if data.invest_losses:
            card.body().addWidget(_section_label("Top losses"))
            for h in data.invest_losses:
                card.body().addWidget(_perf_row(h, data.display_ccy, "negative"))
        return card


def _muted(text: str) -> QLabel:
    lbl = QLabel(text)
    tokens.themed(lbl, "color: {subtle}; font-style: italic;")
    return lbl


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    tokens.themed(lbl, "color: {muted}; font-size: 11px; font-weight: 600;")
    return lbl


def _perf_row(h, ccy: str, right_token: str) -> _Row:
    label = h.symbol or h.name
    pct = f"  ({h.pct * 100:+.1f}%)" if h.pct is not None else ""
    return _Row(label, _fmt(h.gain, ccy, signed=True) + pct, right_token=right_token)
