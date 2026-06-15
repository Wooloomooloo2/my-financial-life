"""Home / dashboard view (ADR-075, Arc F).

Presentation only: renders the Qt-free ``HomeData`` (from
``home_dashboard.gather_home_data``) into a scrollable grid of cards and emits
navigation signals the register window wires to its existing handlers. Each
card hides when it has nothing to show. ``refresh()`` re-gathers and rebuilds.
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
    """A titled card. Optionally clickable (whole-card navigation)."""
    clicked = Signal()

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("homeCard")
        self.setStyleSheet(
            "#homeCard { background: white; border: 1px solid #e2e8f0; "
            "border-radius: 10px; }"
        )
        self._clickable = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        header = QLabel(title)
        header.setStyleSheet(
            "font-size: 11px; font-weight: 600; color: #64748b; "  # slate-500
            "letter-spacing: 0.04em;"
        )
        lay.addWidget(header)
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
        if self._clickable:
            self.clicked.emit()
        super().mousePressEvent(e)


class _Row(QFrame):
    """A two-column (left/right) row, optionally clickable for navigation."""
    clicked = Signal()

    def __init__(self, left: str, right: str, *, right_color: str = "",
                 clickable: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._clickable = clickable
        if clickable:
            self.setCursor(Qt.PointingHandCursor)
            self.setStyleSheet(":hover { background: #f1f5f9; border-radius: 6px; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 3, 2, 3)
        lay.setSpacing(8)
        l = QLabel(left)
        l.setTextFormat(Qt.PlainText)
        r = QLabel(right)
        if right_color:
            r.setStyleSheet(f"color: {right_color};")
        lay.addWidget(l, stretch=1)
        lay.addWidget(r, stretch=0)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if self._clickable:
            self.clicked.emit()
        super().mousePressEvent(e)


class _AccordionHeader(QFrame):
    """A clickable family header (chevron + label + subtotal) that toggles its
    account rows. Starts collapsed so the Accounts card stays concise."""
    toggled = Signal(bool)

    def __init__(self, title: str, value: str, parent=None) -> None:
        super().__init__(parent)
        self._expanded = False
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(":hover { background: #f1f5f9; border-radius: 6px; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 4, 2, 4)
        lay.setSpacing(6)
        self._chev = QLabel("▸")
        self._chev.setStyleSheet("color: #64748b;")
        self._chev.setFixedWidth(12)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("font-weight: 600; color: #334155;")
        value_lbl = QLabel(value)
        value_lbl.setStyleSheet("font-weight: 600; color: #334155;")
        lay.addWidget(self._chev)
        lay.addWidget(title_lbl, stretch=1)
        lay.addWidget(value_lbl)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        self._expanded = not self._expanded
        self._chev.setText("▾" if self._expanded else "▸")
        self.toggled.emit(self._expanded)
        super().mousePressEvent(e)


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
        self._scroll.setStyleSheet("QScrollArea { background: #f8fafc; }")  # slate-50
        outer.addWidget(self._scroll)
        self._container: Optional[QWidget] = None

    # ── build ──

    def refresh(self) -> None:
        if not self._repo.is_open():
            return
        try:
            data = gather_home_data(self._repo, date.today())
        except Exception:
            return
        container = QWidget()
        container.setStyleSheet("background: #f8fafc;")
        outer = QHBoxLayout(container)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)

        # Two independently-packed columns (greedy-balanced by an approximate
        # per-card height weight) so a tall card never leaves the other column
        # with a big blank gap — the QGridLayout's shared row heights did.
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
        outer.addWidget(left_wrap, 1)
        outer.addWidget(right_wrap, 1)

        self._scroll.setWidget(container)
        self._container = container

    def _build_cards(self, data) -> list:
        return [
            self._net_worth_card(data),
            self._budget_card(data),
            self._accounts_card(data),
            self._bills_card(data),
            self._recent_card(data),
            self._top_payees_card(data),
            self._top_categories_card(data),
            self._investments_card(data),
        ]

    # ── individual cards ──

    def _net_worth_card(self, data) -> _Card:
        card = _Card("NET WORTH")
        card._weight = 3 if data.net_worth_excluded else 2
        big = QLabel(_fmt(data.net_worth, data.display_ccy, decimals=0))
        big.setStyleSheet("font-size: 30px; font-weight: 700; color: #0f172a;")
        card.body().addWidget(big)
        if data.net_worth_excluded:
            note = QLabel(
                f"{data.net_worth_excluded} account"
                f"{'s' if data.net_worth_excluded != 1 else ''} excluded "
                f"(no exchange rate)"
            )
            note.setStyleSheet("color: #b45309; font-size: 11px;")  # amber-700
            card.body().addWidget(note)
        card.make_clickable()
        card.clicked.connect(self.net_worth_requested)
        return card

    def _budget_card(self, data) -> Optional[_Card]:
        b = data.budget
        if b is None:
            return None
        card = _Card(f"BUDGET · {b.month_label.upper()}")
        card._weight = 4
        line = QLabel(
            f"{_fmt(b.spent, b.currency)} of {_fmt(b.planned, b.currency)} spent"
        )
        line.setStyleSheet("font-size: 15px; color: #0f172a;")
        card.body().addWidget(line)
        bar = QProgressBar()
        planned = float(b.planned)
        spent = float(b.spent)
        pct = int(min(100, round(spent / planned * 100))) if planned > 0 else 0
        bar.setRange(0, 100)
        bar.setValue(pct)
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        over = planned > 0 and spent > planned
        colour = "#dc2626" if over else "#2563eb"  # red-600 / blue-600
        bar.setStyleSheet(
            "QProgressBar { background: #e2e8f0; border: none; border-radius: 4px; } "
            f"QProgressBar::chunk {{ background: {colour}; border-radius: 4px; }}"
        )
        card.body().addWidget(bar)
        if over:
            note = QLabel(f"Over by {_fmt(b.spent - b.planned, b.currency)}")
            note.setStyleSheet("color: #dc2626; font-size: 11px;")
            card.body().addWidget(note)
        card.make_clickable()
        card.clicked.connect(self.budget_requested)
        return card

    def _accounts_card(self, data) -> _Card:
        card = _Card("ACCOUNTS")
        # Collapsed, this card is just the family headers — keep its weight
        # small so the column balancer doesn't treat it as a giant block.
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
        card = _Card(title)
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
                colour = "#dc2626" if b.overdue else "#64748b"
                row = _Row(
                    f"{b.label}  ·  {when}",
                    _fmt(b.amount, data.display_ccy),
                    right_color=colour if b.overdue else "",
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
            colour = "#16a34a" if t.amount > 0 else ""  # green-600 for inflow
            row = _Row(
                label, _fmt(t.amount, data.display_ccy, signed=True),
                right_color=colour, clickable=bool(t.account_iri),
            )
            if t.account_iri:
                row.clicked.connect(
                    lambda iri=t.account_iri: self.account_requested.emit(iri)
                )
            card.body().addWidget(row)
        return card

    def _top_payees_card(self, data) -> _Card:
        card = _Card("TOP PAYEES · THIS MONTH")
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
        card = _Card("TOP CATEGORIES · THIS MONTH")
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
                card.body().addWidget(_perf_row(h, data.display_ccy, "#16a34a"))
        if data.invest_losses:
            card.body().addWidget(_section_label("Top losses"))
            for h in data.invest_losses:
                card.body().addWidget(_perf_row(h, data.display_ccy, "#dc2626"))
        return card


def _muted(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("color: #94a3b8; font-style: italic;")  # slate-400
    return lbl


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("color: #64748b; font-size: 11px; font-weight: 600;")
    return lbl


def _perf_row(h, ccy: str, colour: str) -> _Row:
    label = h.symbol or h.name
    pct = f"  ({h.pct * 100:+.1f}%)" if h.pct is not None else ""
    return _Row(label, _fmt(h.gain, ccy, signed=True) + pct, right_color=colour)
