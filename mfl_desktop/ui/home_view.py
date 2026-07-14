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

import shiboken6
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import Repository
from mfl_desktop.home_dashboard import (
    compute_investment_performance,
    compute_net_worth_trend,
    gather_home_data,
)
from mfl_desktop.ui import tokens
from mfl_desktop.ui.chart_helpers import currency_symbol
from mfl_desktop.ui.net_worth_sparkline import NetWorthSparkline

def _sym(ccy: str) -> str:
    """The currency glyph, via the one definition (ADR-165)."""
    return currency_symbol(ccy) if ccy else ""


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
        # afterwards. Note this ordering is necessary but *not* sufficient: Qt
        # itself keeps using the receiver after this returns, so refresh() must
        # also defer the card's destruction (see HomeView.refresh, ADR-149).
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
        # rebuild on navigation), so never touch self after emitting. Qt still
        # touches the receiver after we return, so refresh() defers the actual
        # destruction (ADR-149).
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


class _HomeBgSignals(QObject):
    """Carries the background-computed Home cards back to the main thread. Created
    on the main thread and owned by the runnable that emits it (ADR-156), so the
    cross-thread emit is auto-queued onto the UI event loop and the sender cannot
    outlive-then-predecease its emitter."""
    done = Signal(object)   # {"sig": str, "trend": ... | None, "invest": ... | None}


class _HomeBgRunnable(QRunnable):
    """Computes the Home cards too heavy for the fast path — the net-worth trend
    and investment performance (ADR-150) — off the UI thread in one pass, sharing
    one background Repository connection (a sqlite connection can't cross
    threads). Each card's failure is isolated so one never sinks the other.

    ADR-156: the runnable **owns** its signals object rather than borrowing the
    view's, so a HomeView that dies while a pass is in flight (the window closes)
    doesn't leave the worker holding the sender. Emitting *to* a destroyed
    receiver is safe — Qt disconnects it automatically. This mirrors
    ``_MarketRefreshRunnable`` in register_window.py.

    Ownership alone is not enough, though. On **app shutdown** Qt/PySide destroy
    the C++ QObject regardless of who holds the Python reference, so a pass still
    running when the user quits emits from a deleted sender and raises
    ``RuntimeError: Signal source has been deleted`` on the worker thread. Every
    emit therefore goes through :meth:`_emit`, which checks the sender is still
    alive. Draining the thread pool on quit was rejected — it would block the UI
    for the length of a full net-worth trend."""

    def __init__(self, db_path, display_ccy, today, sig) -> None:
        super().__init__()
        self._db_path = db_path
        self._ccy = display_ccy
        self._today = today
        self._sig = sig
        self.signals = _HomeBgSignals()

    def _emit(self, payload: dict) -> None:
        """Hand the result back, unless the app is being torn down under us.

        ``isValid`` is the shiboken check for "the C++ object behind this Python
        wrapper still exists". The ``try`` is not redundant: shutdown races this
        worker, so the object can be destroyed between the check and the emit.
        Dropping the payload is exactly right — the receiver is gone too."""
        try:
            if shiboken6.isValid(self.signals):
                self.signals.done.emit(payload)
        except RuntimeError:
            pass

    def run(self) -> None:
        trend = invest = None
        try:
            bg = Repository(self._db_path)
        except Exception:
            self._emit({"sig": self._sig, "trend": None, "invest": None})
            return
        try:
            try:
                trend = compute_net_worth_trend(bg, self._today, self._ccy)
            except Exception:
                trend = None
            try:
                invest = compute_investment_performance(bg, self._today, self._ccy)
            except Exception:
                invest = None
        finally:
            bg.close()
        self._emit({"sig": self._sig, "trend": trend, "invest": invest})


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

        # Heavy Home cards (net-worth trend + investment performance, ADR-150)
        # computed off-thread and cached against a cheap data signature, so they
        # paint synchronously while the data is unchanged and only recompute when
        # it moves. Both come back together from one background pass.
        self._nw_trend = None            # NetWorthTrend | None
        self._invest_perf = None         # InvestmentPerf | None
        self._bg_sig: Optional[str] = None
        self._bg_pending: Optional[str] = None
        # ADR-156: what the currently-rendered dashboard was built from. Lets
        # refresh_if_stale() skip a rebuild the user cannot tell apart from what
        # is already on screen.
        self._rendered_token: Optional[tuple] = None
        # ADR-160: the last gathered HomeData and the token it was gathered for,
        # so folding in the off-thread cards doesn't re-run the whole query pass.
        self._last_data = None
        self._last_token: Optional[tuple] = None

    def set_repo(self, repo) -> None:
        """Point the dashboard at a different file (File ▸ Open swaps the live
        repo — ADR-092). Without this the view keeps reading the old, now-closed
        repo and shows stale data until restart. Caller refreshes after."""
        self._repo = repo
        # The generation counter is per-Repository, so a token from the old file
        # says nothing about the new one. Drop it: the next refresh must rebuild.
        self._rendered_token = None
        # And the cached data belongs to the OLD file — never reuse it (ADR-160).
        self._last_data = None
        self._last_token = None

    # ── build ──

    def _freshness_token(self) -> tuple:
        """What the dashboard's contents depend on (ADR-156). The data generation
        covers every edit; ``date.today()`` covers the date-relative cards (bills
        due, "last 30 days") on an app left open across midnight — the same
        rollover the Schedules cue re-checks on activation (ADR-063)."""
        return (self._repo.data_generation(), date.today())

    def refresh_if_stale(self) -> bool:
        """Rebuild only if the result would actually differ from what's on screen.

        The register window refreshes Home whenever it regains activation, so that
        edits made in another window show up (ADR-075). But activation fires on
        *every* return of focus — closing a report, switching to a register,
        alt-tabbing — and a full rebuild is expensive (it re-derives every account
        value and reconstructs the whole card tree). Overwhelmingly it lands on
        data that has not moved, and paints an identical dashboard.

        Comparing the freshness token first keeps the ADR-075 guarantee (a real
        edit still bumps the generation, so it still redraws) while making the
        common no-op case cost a couple of microseconds. Returns whether it
        rebuilt."""
        if not self._repo.is_open():
            return False
        if self._freshness_token() == self._rendered_token:
            return False
        self.refresh()
        return True

    def refresh(self, *, reuse_data: bool = False) -> None:
        """Rebuild the dashboard.

        ``reuse_data`` (ADR-160) skips the re-gather and redraws from the data of
        the last render. Only for a caller that knows the *data* hasn't moved and
        only the presentation has — specifically :meth:`_on_bg_ready`, which folds
        the off-thread cards into a dashboard it just drew. ``gather_home_data``
        is ~85% of a refresh (480ms of 553ms against the live file), so redoing it
        to add a sparkline was most of a second of pure waste. Guarded on the
        freshness token anyway: if the data *did* move under us, we re-gather.
        """
        if not self._repo.is_open():
            return
        # Sampled BEFORE the rebuild, so a write that lands while we're building
        # (a background price refresh) leaves the token stale rather than being
        # wrongly recorded as already on screen.
        token = self._freshness_token()
        if reuse_data and self._last_data is not None and self._last_token == token:
            data = self._last_data
        else:
            try:
                data = gather_home_data(self._repo, date.today())
            except Exception:
                return
            self._last_data = data
            self._last_token = token
        container = QWidget()
        tokens.themed(container, "background: {canvas};")
        root = QVBoxLayout(container)
        root.setContentsMargins(16, 4, 16, 16)
        root.setSpacing(16)

        # ADR-119: the net-worth hero spans the full width above the grid.
        # ADR-150: its 12-month trend is computed off-thread; use the cached
        # series if it matches the current data, else render number-only now and
        # kick off a background compute that folds the sparkline in when ready.
        sig = self._bg_sig_for(data)
        have_for_sig = self._bg_sig == sig           # computed (results may be None)
        trend = self._nw_trend if have_for_sig else None
        invest = self._invest_perf if have_for_sig else None
        root.addWidget(self._hero_card(data, trend))
        if not have_for_sig and self._bg_pending != sig:
            self._start_bg_compute(sig, data.display_ccy)

        # Two independently-packed columns (greedy-balanced by an approximate
        # per-card height weight) so a tall card never leaves the other column
        # with a big blank gap.
        left = QVBoxLayout()
        right = QVBoxLayout()
        left.setSpacing(16)
        right.setSpacing(16)
        left_w = right_w = 0
        for card in self._build_cards(data, invest):
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

        # QScrollArea::setWidget *deletes* the widget it replaces, immediately.
        # refresh() can run while one of this container's own cards is still on
        # the stack mid-mouse-event — a card's clicked slot opens a modal dialog
        # (Schedules), and closing it re-activates the window, whose
        # ActivationChange handler refreshes Home. Destroying the card there
        # leaves QApplication::notify dereferencing a freed receiver when the
        # click unwinds (ADR-149). Take the old container out first and defer
        # its destruction to the event loop, once Qt has finished with it.
        old = self._scroll.takeWidget()
        self._scroll.setWidget(container)
        if old is not None:
            old.deleteLater()
        self._container = container
        self._rendered_token = token

    def _build_cards(self, data, invest=None) -> list:
        return [
            self._budget_card(data),
            self._accounts_card(data),
            self._bills_card(data),
            self._recent_card(data),
            self._top_payees_card(data),
            self._top_categories_card(data),
            self._investments_card(data, invest),
        ]

    # ── individual cards ──

    def _hero_card(self, data, trend=None) -> _Card:
        """The full-width net-worth hero (ADR-119) — big number + accent left
        edge (via the homeHeroCard object name). When a 12-month trend is
        available (ADR-150) it fills the right of the card as a sparkline, and a
        change indicator gives the number direction; otherwise it degrades to
        the number-only card and the trend arrives on the next refresh."""
        card = _Card("NET WORTH", action="Net worth →")
        card.setObjectName("homeHeroCard")

        # Two columns: the figures on the left, the trend on the right.
        row = QWidget()
        row_l = QHBoxLayout(row)
        row_l.setContentsMargins(0, 0, 0, 0)
        row_l.setSpacing(24)

        figures = QWidget()
        fig_l = QVBoxLayout(figures)
        fig_l.setContentsMargins(0, 0, 0, 0)
        fig_l.setSpacing(4)

        big = QLabel(_fmt(data.net_worth, data.display_ccy, decimals=0))
        tokens.themed(big, "font-size: 44px; font-weight: 700; color: {text};")
        # A touch of negative tracking sets the money apart from body text — a
        # small nod to the "give numerals a voice" direction (Qt QSS has no
        # letter-spacing, so it's set on the font).
        big_font = big.font()
        big_font.setLetterSpacing(QFont.PercentageSpacing, 97)
        big.setFont(big_font)
        fig_l.addWidget(big)

        # Change summary — a 30-day and a 12-month window, coloured by direction
        # (ADR-150). The chart shows the 12-month shape; these summarise it.
        if trend is not None:
            for change, pct, period in (
                (trend.change_30d, trend.change_30d_pct, "last 30 days"),
                (trend.change_year, trend.change_year_pct, "last 12 months"),
            ):
                lbl = self._delta_label(change, pct, period, data.display_ccy)
                if lbl is not None:
                    fig_l.addWidget(lbl)

        n_accts = sum(len(g.accounts) for g in data.account_groups)
        if n_accts:
            sub = QLabel(f"across {n_accts} account{'s' if n_accts != 1 else ''}")
            tokens.themed(sub, "color: {muted}; font-size: 12px;")
            fig_l.addWidget(sub)
        if data.net_worth_excluded:
            note = QLabel(
                f"{data.net_worth_excluded} account"
                f"{'s' if data.net_worth_excluded != 1 else ''} excluded "
                f"(no exchange rate)"
            )
            tokens.themed(note, "color: {warning}; font-size: 11px;")
            fig_l.addWidget(note)
        fig_l.addStretch(1)

        row_l.addWidget(figures, 0)

        if trend is not None and len(trend.points) >= 2:
            spark = NetWorthSparkline()
            spark.render(trend.points)
            spark.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            row_l.addWidget(spark, 1)
        else:
            row_l.addStretch(1)

        card.body().addWidget(row)
        card.make_clickable()
        card.clicked.connect(self.net_worth_requested)
        return card

    def _delta_label(self, change, pct, period: str, ccy: str):
        """A coloured change line — '▲ £X (+Y%) · <period>' — for one window.
        A sub-unit drift reads as flat (muted, no arrow); an undefined change
        returns None so the caller can skip it."""
        if change is None:
            return None
        if abs(change) < Decimal("0.5"):
            lbl = QLabel(f"No change · {period}")
            tokens.themed(lbl, "color: {muted}; font-size: 13px; font-weight: 600;")
            return lbl
        up = change > 0
        arrow = "▲" if up else "▼"
        token = "positive_strong" if up else "negative"
        amount = _fmt(abs(change), ccy, decimals=0)
        pct_s = ""
        if pct is not None:
            pct_s = f" ({'+' if up else '−'}{abs(pct) * 100:.1f}%)"
        lbl = QLabel(f"{arrow} {amount}{pct_s} · {period}")
        tokens.themed(
            lbl, "color: {%s}; font-size: 13px; font-weight: 600;" % token
        )
        return lbl

    # ── heavy off-thread cards (ADR-150) ──

    def _bg_sig_for(self, data) -> str:
        """A cheap signature identifying the data the off-thread cards were
        computed for. It changes exactly when they would — net worth moves (which
        also captures a price change, since it feeds investment value), an account
        is added/removed, the display currency changes, or the day rolls over."""
        n_accts = sum(len(g.accounts) for g in data.account_groups)
        return f"{data.net_worth}|{n_accts}|{data.display_ccy}|{date.today().isoformat()}"

    def _start_bg_compute(self, sig: str, display_ccy: str) -> None:
        db_path = getattr(self._repo, "db_path", None)
        if db_path is None:
            return
        self._bg_pending = sig
        runnable = _HomeBgRunnable(str(db_path), display_ccy, date.today(), sig)
        # Connect to the runnable's OWN signals object (ADR-156). Qt drops the
        # connection automatically if this view is destroyed first, so a pass
        # still in flight when Home goes away lands harmlessly instead of
        # emitting from a deleted sender.
        runnable.signals.done.connect(self._on_bg_ready)
        QThreadPool.globalInstance().start(runnable)

    def _on_bg_ready(self, payload) -> None:
        """Background cards landed (main thread, queued). Cache them and repaint
        Home; refresh() will find them cached for the current signature and draw
        them — or, if the data has since moved, miss and recompute.

        ADR-160: ``reuse_data`` — only the trend/perf cards changed, and this is a
        dashboard we ourselves just drew, so re-running the whole query pass to
        add a sparkline is waste. refresh() still re-gathers if the data actually
        moved while the worker was out."""
        sig = payload.get("sig")
        if self._bg_pending == sig:
            self._bg_pending = None
        self._nw_trend = payload.get("trend")
        self._invest_perf = payload.get("invest")
        self._bg_sig = sig
        if self._repo.is_open():
            self.refresh(reuse_data=True)

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
        # The card names the period it actually covers (ADR-163): "THIS MONTH"
        # normally, or the month it fell back to when this one has no spending.
        card = _Card(
            f"TOP PAYEES · {data.spend_period_label.upper()}", action="Payees →"
        )
        card._weight = len(data.top_payees) + 1
        if not data.top_payees:
            card.body().addWidget(_muted("No spending recorded yet."))
        else:
            for p in data.top_payees:
                card.body().addWidget(
                    _Row(p.label, _fmt(p.amount, data.display_ccy))
                )
        card.make_clickable()
        card.clicked.connect(self.payee_report_requested)
        return card

    def _top_categories_card(self, data) -> _Card:
        card = _Card(
            f"TOP CATEGORIES · {data.spend_period_label.upper()}",
            action="Spending →",
        )
        card._weight = len(data.top_categories) + 1
        if not data.top_categories:
            card.body().addWidget(_muted("No spending recorded yet."))
        else:
            for ct in data.top_categories:
                card.body().addWidget(
                    _Row(ct.label, _fmt(ct.amount, data.display_ccy))
                )
        card.make_clickable()
        card.clicked.connect(self.spending_report_requested)
        return card

    def _investments_card(self, data, invest=None) -> Optional[_Card]:
        # Period-scoped performance (ADR-150) arrives from the background pass;
        # until it lands (or when there are no investments) the card is absent.
        if invest is None:
            return None
        ccy = data.display_ccy
        card = _Card("INVESTMENT PERFORMANCE")
        card._weight = 3 + len(invest.gainers) + len(invest.losers)

        # Two portfolio windows, matching the net-worth hero.
        for change, pct, period in (
            (invest.return_30d, invest.pct_30d, "last 30 days"),
            (invest.return_12m, invest.pct_12m, "last 12 months"),
        ):
            lbl = self._delta_label(change, pct, period, ccy)
            if lbl is not None:
                card.body().addWidget(lbl)

        # True-return top movers over the 12-month window.
        if invest.gainers or invest.losers:
            card.body().addWidget(_section_label("Top movers · 12 months"))
            for h in invest.gainers:
                card.body().addWidget(_perf_row(h, ccy, "positive"))
            for h in invest.losers:
                card.body().addWidget(_perf_row(h, ccy, "negative"))
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
