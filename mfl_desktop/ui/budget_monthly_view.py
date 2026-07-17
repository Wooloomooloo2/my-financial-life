"""Budget monthly view (ADR-058 R3, principle 11) — one month in focus.

A complement to the annual matrix: the same `BudgetMatrix` (single source of
budget truth) rendered as **per-envelope progress bars** for one focused
month, plus a **projected burn-down** chart at the top. The budget stays
editable here (click an amount → copy-forward prompt) and double-clicking a
bar drills into the transactions behind it — both routed back through the
owning `BudgetWindow` via the `edit_cb` / `drill_cb` callbacks so there's one
edit path and one drill path.

Layout (top-down):

- **Month selector** — ◀ / month / ▶, defaulting to today's month, clamped to
  the budget's range; plus the soft Unallocated indicator for that month.
- **Burn-down** — a scope combo (Whole budget, or any expense envelope) over a
  `BurnDownChart` showing Actual vs Ideal vs the forward Projection.
- **Envelope list** — scrollable, grouped Income / Expenses / Transfers; each
  budgeted line is a `spent / available` bar (green → amber → red), with the
  ↻ glyph on rolling lines and a muted Unbudgeted row per section. Rows carry
  the category tree (ADR-170): a group header rolls its subtree up and
  collapses on click, children indent under it, and 'Everything else' holds
  what no budgeted child claimed. The collapse set is owned by the enclosing
  `BudgetWindow` and shared with the annual matrix, so a group collapsed on
  one view is collapsed on both.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPen,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop import budget_calc as bc
from mfl_desktop.db.repository import Repository
from mfl_desktop.ui import tokens
from mfl_desktop.ui.chart_helpers import currency_symbol
from mfl_desktop.ui.ui_fonts import set_pt

_MONTH_ABBR = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Bar-fill colours resolved from the design tokens at paint-time so they follow
# the active light/dark theme (ADR-076). Each token's light value equals the hex
# it replaced, so light mode is unchanged.
def _over() -> QColor:        return QColor(tokens.c("negative"))         # over budget (expense/transfer)
def _near() -> QColor:        return QColor(tokens.c("caution"))          # ≥85% spent
def _under() -> QColor:       return QColor(tokens.c("positive"))         # comfortably under / income progress
def _over_income() -> QColor: return QColor(tokens.c("positive_strong"))  # income beat its target (good)
def _track() -> QColor:       return QColor(tokens.c("border"))           # empty track
def _muted_fill() -> QColor:  return QColor(tokens.c("border_strong"))    # muted/unbudgeted bar fill

# Text inks. Resolved at render-time from the tokens, not frozen as module
# constants: this view used to carry `_MUTED`/`_GREEN_TXT`/`_RED_TXT` as three
# light-theme hexes — the last three on ADR-167's ratchet for this module — so
# in dark mode its remainder text was a light-theme green on the dark canvas
# (ADR-171). Each token's light value equals the hex it replaced.
def _muted_ink() -> str:      return tokens.c("muted")
def _good_ink() -> str:       return tokens.c("positive_strong")
def _bad_ink() -> str:        return tokens.c("negative_strong")

_ZERO = Decimal("0.00")

# One tree level of indent in the envelope list (ADR-170) — matches the annual
# matrix's step so the two views read as the same tree.
_INDENT = "    "


def _month_label(month: str) -> str:
    return f"{_MONTH_ABBR[int(month[5:7])]} {month[:4]}"


def _fmt(value: Decimal) -> str:
    return f"{value:,.2f}"


def _money(ccy: str, value: Decimal) -> str:
    """'£822.64' / '-£2,387.36' — the glyph, not the ISO code (ADR-171).

    This view was the last surface printing money as ``GBP 822.64``. It escaped
    ADR-159 and ADR-165 because it has no private currency table to find: it
    simply printed ``f"{ccy} {amount}"``, which is the same defect with nothing
    to grep for. ``currency_symbol`` is the app's one definition of the glyph
    and already falls back to a spaced code for a currency we have no symbol
    for. The sign goes *outside* — "-£20", never "£-20".
    """
    sign = "-" if value < 0 else ""
    return f"{sign}{currency_symbol(ccy)}{_fmt(abs(value))}"


def _remainder(kind: str, diff: Decimal, ccy: str) -> tuple[str, str]:
    """(text, ink) for a row's headline remainder — ``diff`` is ADR-058's
    favourable-signed diff, so positive is always good (ADR-171).

    The old view printed a bare signed number (``+7,553.13``) and left the
    reader to decode what its sign meant *for this kind of row* — and got it
    wrong for income, where under-earning showed as an alarming red deficit
    when it is simply the month not being over yet. Saying the word removes
    both problems: an expense has money **left** or is **over**; income is
    **above plan** or has some **to go**.
    """
    money = _money(ccy, abs(diff))
    if kind == "income":
        if diff >= 0:
            return f"{money} above plan", _good_ink()
        # Not red: earning less than planned part-way through a month is the
        # normal state of every month, not an error.
        return f"{money} to go", _muted_ink()
    if diff < 0:
        return f"{money} over", _bad_ink()
    return f"{money} left", _good_ink()


# The three right-hand columns. Fixed widths so every row's numbers land on the
# same axis and the eye can run straight down them — the old 190/160/78 split
# predates both the tree indent (which eats into the name) and the currency
# glyph (which widens every amount), and clipped both (ADR-171).
_NAME_W = 248
_AMOUNT_W = 190
_REMAINDER_W = 126


def _bold() -> QFont:
    f = QFont()
    f.setBold(True)
    return f


def _size_name(label: QLabel) -> None:
    """Fix the name column and elide what won't fit, keeping the full text in
    the tooltip. A QLabel *clips* by default — 'Digital Subscriptions' simply
    vanished mid-word with nothing to say it had — and an indented tree makes
    the long names longer still."""
    label.setFixedWidth(_NAME_W)
    label.setWordWrap(False)
    full = label.text()
    metrics = QFontMetrics(label.font())
    elided = metrics.elidedText(full, Qt.ElideRight, _NAME_W - 4)
    if elided != full:
        label.setText(elided)
        if not label.toolTip():
            label.setToolTip(full.strip())


def _size_amount(label: QLabel) -> None:
    label.setFixedWidth(_AMOUNT_W)
    label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)


def _size_remainder(label: QLabel) -> None:
    label.setFixedWidth(_REMAINDER_W)
    label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)


def _pacing_target(cell) -> Decimal:
    """What the burn-down paces against: **this month's allocation** (ADR-172).

    Not ``available``. Available is allocation *plus accumulated rollover*, and
    pacing against it is why the chart never worked: on a real budget six
    months of unspent surplus had inflated it to **5.4× the month's plan**, so
    the pacing line insisted you should have spent £10,909 by the 17th of a
    month you had assigned £3,673 to — and the budget line, setting the y-axis,
    squashed the actual spending into the bottom 7% of the chart.

    A rollover surplus is a **buffer, not a target**: you don't aim to spend
    it, so it has no business in the pacing. It is still money you can reach —
    the row above says so (``£1,460.00 of £19,892.89``, ADR-171) and the
    verdict caption names it — but the chart's question is "am I pacing this
    month's plan", and the plan is the allocation.

    Symmetrically, a carried-in *deficit* does not lower the target either. The
    plan is the plan; the debt is reported on the row, where ADR-171 put it.
    """
    return cell.allocation if cell is not None else _ZERO


def _budget_tooltip(cell, ccy: str) -> str:
    """The click-to-edit hint, plus the carry reconciliation when there is one.

    The carry used to be printed inline as `(+6,571.13)`, which made the row's
    headline four numbers deep and still didn't explain itself. Here it has
    room to say what it means — the same move ADR-124 made when the annual
    grid's inline carry annotation was overflowing its column.
    """
    base = "Click to edit this month's budget"
    carry = cell.carry_in
    if carry == 0:
        return base
    if carry > 0:
        return (
            f"Budgeted {_money(ccy, cell.allocation)} + "
            f"{_money(ccy, carry)} rolled over = "
            f"{_money(ccy, cell.available)} available this month.\n{base}"
        )
    return (
        f"Budgeted {_money(ccy, cell.allocation)} − "
        f"{_money(ccy, abs(carry))} overspend carried in = "
        f"{_money(ccy, cell.available)} available this month.\n{base}"
    )


class _ClickLabel(QLabel):
    """A QLabel that emits ``clicked`` on a left press — the edit affordance
    on the amount text."""

    clicked = Signal()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)


class _Bar(QWidget):
    """A horizontal spent/available fill bar. Double-click drills (the bar is
    the drill target so it never collides with the amount's single-click
    edit). ``muted`` paints a flat full bar for the Unbudgeted row."""

    doubleClicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._fraction = 0.0
        self._over = False
        self._muted = False
        self._income = False
        self.setMinimumHeight(18)
        self.setMaximumHeight(18)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_fill(
        self, fraction: float, over: bool,
        muted: bool = False, income: bool = False,
    ) -> None:
        self._fraction, self._over = fraction, over
        self._muted, self._income = muted, income
        self.update()

    def _fill_colour(self) -> QColor:
        if self._muted:
            return _muted_fill()
        if self._income:
            # Earning more than planned is good, never bad — income bars are
            # never red and never amber. A deeper green marks beating the
            # target; normal green is progress toward it.
            return _over_income() if self._over else _under()
        if self._over:
            return _over()
        if self._fraction >= 0.85:
            return _near()
        return _under()

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(ev)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.NoPen)
        p.setBrush(_track())
        p.drawRoundedRect(r, 4, 4)
        frac = 1.0 if self._muted else max(0.0, min(self._fraction, 1.0))
        if frac > 0:
            fr = QRectF(r.left(), r.top(), r.width() * frac, r.height())
            p.setBrush(self._fill_colour())
            p.drawRoundedRect(fr, 4, 4)
        p.end()


class BudgetMonthlyView(QWidget):
    """Single-month progress view. Driven by ``set_data(budget, matrix)`` from
    the owning window; edits + drills route back via the two callbacks."""

    def __init__(
        self, repo: Repository, *, edit_cb, drill_cb, collapse_cb, parent=None,
    ):
        super().__init__(parent)
        self._repo = repo
        self._edit_cb = edit_cb       # (line_id, month, Decimal) -> bool
        self._drill_cb = drill_cb     # (mode, target_cat, kind, month, label)
        # ADR-170: ``collapse_cb(key)`` flips + persists a collapse key in the
        # owning window, which then pushes the new set back via set_collapsed —
        # so a group collapsed on the annual matrix is collapsed here too.
        self._collapse_cb = collapse_cb
        self._collapsed: set[str] = set()
        self._budget = None
        self._matrix: Optional[bc.BudgetMatrix] = None
        self._month: Optional[str] = None
        self._scope_cat: Optional[int] = None   # burn-down scope; None = whole

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)

        # Month selector + unallocated.
        sel = QHBoxLayout()
        self._prev = QPushButton("◀")
        self._prev.setFixedWidth(34)
        self._prev.clicked.connect(lambda: self._step_month(-1))
        self._next = QPushButton("▶")
        self._next.setFixedWidth(34)
        self._next.clicked.connect(lambda: self._step_month(1))
        self._month_lbl = QLabel("")
        self._month_lbl.setAlignment(Qt.AlignCenter)
        mf = QFont()
        mf.setBold(True)
        set_pt(mf, 11)
        self._month_lbl.setFont(mf)
        self._month_lbl.setMinimumWidth(170)
        sel.addWidget(self._prev)
        sel.addWidget(self._month_lbl)
        sel.addWidget(self._next)
        sel.addSpacing(16)
        self._unalloc = QLabel("")
        self._unalloc.setTextFormat(Qt.RichText)
        sel.addWidget(self._unalloc)
        sel.addStretch(1)
        root.addLayout(sel)

        # Burn-down block. The chart now genuinely burns *down* (ADR-172), so
        # the label finally describes the picture instead of contradicting it.
        from mfl_desktop.ui.burn_down_chart import BurnDownChart
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Burn-down:"))
        self._scope = QComboBox()
        self._scope.setMinimumWidth(200)
        self._scope.currentIndexChanged.connect(self._on_scope_changed)
        scope_row.addWidget(self._scope)
        scope_row.addSpacing(16)
        # The verdict — the chart's headline, in words (ADR-164's principle:
        # lead with what it says, not with a shape to interpret). It lives in
        # this row rather than inside the paint so it costs no chart height and
        # rides the token layer like any other label.
        self._verdict = QLabel("")
        self._verdict.setTextFormat(Qt.RichText)
        scope_row.addWidget(self._verdict)
        scope_row.addStretch(1)
        root.addLayout(scope_row)
        self._chart = BurnDownChart()
        root.addWidget(self._chart)

        # Envelope list (scrollable).
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._list = QWidget()
        self._list_lay = QVBoxLayout(self._list)
        self._list_lay.setContentsMargins(0, 4, 0, 4)
        self._list_lay.setSpacing(2)
        self._scroll.setWidget(self._list)
        root.addWidget(self._scroll, stretch=1)

    # ── data ──

    def set_collapsed(self, collapsed: set[str]) -> None:
        """Adopt the window's collapse set and redraw (ADR-170) — the path for
        a toggle made on the *annual* matrix, where the data hasn't changed."""
        self._collapsed = set(collapsed)
        if self._matrix is not None:
            self._render_month()

    def set_data(
        self, budget, matrix: bc.BudgetMatrix,
        collapsed: Optional[set[str]] = None,
    ) -> None:
        if collapsed is not None:
            self._collapsed = set(collapsed)
        self._budget = budget
        self._matrix = matrix
        months = matrix.months
        if self._month not in months:
            self._month = (
                matrix.today_month if matrix.today_month in months
                else (months[0] if months else None)
            )
        self._rebuild_scope_combo()
        self._render_month()

    def _rebuild_scope_combo(self) -> None:
        """Whole budget + one entry per expense envelope (the lines a burn-down
        is meaningful for). Preserve the current scope if it still exists."""
        prev = self._scope_cat
        self._scope.blockSignals(True)
        self._scope.clear()
        self._scope.addItem("Whole budget", None)
        for section in self._matrix.sections:
            if section.kind != "expense":
                continue
            for row in section.rows:
                # ADR-170: a group and its 'Everything else' share a
                # category_id, so listing both would put the same envelope in
                # the combo twice. Skip the residual and keep the header —
                # scoping a burn-down to a whole group is the useful one.
                if row.row_kind == "residual":
                    continue
                if not row.is_unbudgeted and row.category_id is not None:
                    self._scope.addItem(
                        f"{_INDENT * row.depth}{row.label}", row.category_id,
                    )
        idx = 0
        if prev is not None:
            found = self._scope.findData(prev)
            idx = found if found >= 0 else 0
        self._scope.setCurrentIndex(idx)
        self._scope_cat = self._scope.currentData()
        self._scope.blockSignals(False)

    def _on_scope_changed(self) -> None:
        self._scope_cat = self._scope.currentData()
        self._update_burndown()

    def _step_month(self, delta: int) -> None:
        if not self._matrix:
            return
        months = self._matrix.months
        i = months.index(self._month) + delta
        if 0 <= i < len(months):
            self._month = months[i]
            self._render_month()

    # ── render ──

    def _render_month(self) -> None:
        if not self._matrix or self._month is None:
            return
        months = self._matrix.months
        i = months.index(self._month)
        self._month_lbl.setText(_month_label(self._month))
        self._prev.setEnabled(i > 0)
        self._next.setEnabled(i < len(months) - 1)

        # Money reads as '£5,000.00', not 'GBP 5,000.00' (ADR-171), and the
        # colour is resolved now rather than frozen — this is rich text, so its
        # ink lives in the HTML where `tokens.themed` cannot reach it (the same
        # trap ADR-161 found in the annual window's identical line).
        ccy = self._matrix.display_ccy or ""
        pool = self._matrix.pool
        assigned = self._matrix.assigned_by_month[i]
        unalloc = pool - assigned
        colour = _bad_ink() if unalloc < 0 else _good_ink()
        self._unalloc.setText(
            f"Pool: <b>{_money(ccy, pool)}</b> &nbsp;·&nbsp; "
            f"Assigned: <b>{_money(ccy, assigned)}</b> &nbsp;·&nbsp; "
            f"Unallocated: <b style='color:{colour}'>"
            f"{_money(ccy, unalloc)}</b>"
        )

        self._rebuild_rows(i)
        self._update_burndown()

    def _rebuild_rows(self, mi: int) -> None:
        _clear_layout(self._list_lay)
        for section in self._matrix.sections:
            # Goals already have their own strip above the view (R4b) — don't
            # repeat them as envelope bars here.
            if section.kind == "goals":
                continue
            skey = bc.section_key(section.kind)
            self._list_lay.addWidget(self._section_header(section, skey))
            if skey in self._collapsed:
                continue
            for row in bc.visible_rows(section.rows, self._collapsed):
                self._list_lay.addWidget(self._envelope_row(row, mi))
            # Count top-level rows: a lone group's header already rolls its
            # children up, so a subtotal beneath it would restate it (ADR-170).
            if sum(1 for r in section.rows if r.depth == 0) >= 2:
                self._list_lay.addWidget(self._subtotal_row(section, mi))
        self._list_lay.addStretch(1)

    def _section_header(self, section, key: str) -> QWidget:
        """A clickable section header — click anywhere to collapse (ADR-170).
        Unlike the annual matrix's header row this carries no editable cells,
        so the whole strip is a safe click target."""
        collapsed = key in self._collapsed
        lbl = _ClickLabel(
            f"{'▸' if collapsed else '▾'}  {section.title.upper()}"
        )
        f = QFont()
        f.setBold(True)
        set_pt(f, 9)
        lbl.setFont(f)
        tokens.themed(lbl, "color:{muted_strong}; background:{surface_alt}; padding:4px 6px;")
        lbl.clicked.connect(lambda k=key: self._collapse_cb(k))
        return lbl

    def _envelope_row(self, row: bc.MatrixRow, mi: int) -> QWidget:
        cell = row.cells[mi]
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 1, 6, 1)
        lay.setSpacing(8)

        # Name (+ ↻ for rolling lines). ADR-170: indent by tree depth, and a
        # group header gets a chevron and clicks to collapse.
        label = row.label
        if row.is_editable and row.rollover == "accumulate":
            label += "  ↻"
        gkey = bc.row_group_key(row)
        if gkey is not None:
            chevron = "▸" if gkey in self._collapsed else "▾"
            name = _ClickLabel(f"{_INDENT * row.depth}{chevron}  {label}")
            name.setToolTip(
                "The total for this group — its own ‘Everything else’ plus "
                "every budgeted line beneath it. Click to collapse."
            )
            name.clicked.connect(lambda k=gkey: self._collapse_cb(k))
            gf = QFont()
            gf.setBold(True)
            name.setFont(gf)
        else:
            name = QLabel(f"{_INDENT * row.depth}{label}")
        _size_name(name)
        if row.is_unbudgeted or row.row_kind == "residual":
            name.setStyleSheet(f"color:{_muted_ink()};")
        lay.addWidget(name)

        bar = _Bar()
        bar.doubleClicked.connect(
            lambda r=row: self._drill_row(r)
        )
        lay.addWidget(bar, stretch=1)

        ccy = self._matrix.display_ccy or ""
        if row.is_unbudgeted:
            bar.set_fill(1.0, over=False, muted=True)
            amt = QLabel(f"{_money(ccy, cell.actual)} spent")
            amt.setStyleSheet(f"color:{_muted_ink()};")
            _size_amount(amt)
            lay.addWidget(amt)
            lay.addSpacing(_REMAINDER_W + 8)
            return w

        available = cell.available
        actual = cell.actual
        over = (available > 0 and actual > available) or (
            available <= 0 and actual > 0
        )
        frac = (
            float(actual) / float(available) if available > 0
            else (1.0 if actual > 0 else 0.0)
        )
        bar.set_fill(frac, over=over, income=(row.kind == "income"))

        # The headline pair. Two numbers, not four: the old row printed
        # `spent / available (carry)` *and* a signed diff column — where the
        # diff is just available − spent, and the carry annotation restated
        # what the diff already said. Carry moves to the tooltip, exactly as
        # ADR-124 did for the annual grid's Budget cell (ADR-171).
        #
        # A non-positive `available` gets different words. Rollover carries an
        # overspend *backwards* into next month, so available goes negative and
        # `32.99 / -158.63` is not a sentence — there is no budget to be "of".
        # Say what is true instead: what was spent, and by how much it is over.
        if available > 0:
            text = f"{_money(ccy, actual)} of {_money(ccy, available)}"
        else:
            text = f"{_money(ccy, actual)} spent"
        if row.is_editable:
            amt = _ClickLabel(text)
            amt.setToolTip(_budget_tooltip(cell, ccy))
            amt.clicked.connect(
                lambda lid=row.line_id, lbl=row.label, cur=cell.allocation:
                self._edit_line(lid, lbl, cur)
            )
        else:
            # A group's roll-up is a sum with no line to write to — offering a
            # click-to-edit here would promise an edit that cannot land.
            amt = QLabel(text)
            amt.setToolTip(
                "A group total — edit the lines beneath it, or its "
                "‘Everything else’ line."
            )
            amt.setFont(_bold())
        _size_amount(amt)
        lay.addWidget(amt)

        dtxt, ink = _remainder(row.kind, cell.diff, ccy)
        dl = QLabel(dtxt)
        dl.setStyleSheet(f"color:{ink};")
        if row.is_group:
            dl.setFont(_bold())
        _size_remainder(dl)
        lay.addWidget(dl)
        return w

    def _subtotal_row(self, section: bc.MatrixSection, mi: int) -> QWidget:
        cell = section.subtotal[mi]
        ccy = self._matrix.display_ccy or ""
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(8)
        tokens.themed(w, "border-top: 1px solid {border};")
        name = QLabel(f"{section.title} — total")
        name.setFont(_bold())
        _size_name(name)
        lay.addWidget(name)
        lay.addStretch(1)
        amt = QLabel(
            f"{_money(ccy, cell.actual)} of {_money(ccy, cell.available)}"
        )
        amt.setFont(_bold())
        _size_amount(amt)
        lay.addWidget(amt)
        dtxt, ink = _remainder(section.kind, cell.diff, ccy)
        dl = QLabel(dtxt)
        dl.setFont(_bold())
        dl.setStyleSheet(f"color:{ink};")
        _size_remainder(dl)
        lay.addWidget(dl)
        return w

    # ── burn-down ──

    def _update_burndown(self) -> None:
        if not self._matrix or self._month is None:
            self._chart.set_data(None)
            return
        month = self._month
        mi = self._matrix.months.index(month)
        ptxns = self._repo.list_perimeter_txns(
            self._budget.id, f"{month}-01", f"{month}-31",
        )
        kind_map = self._repo.category_kind_map()
        # ADR-094: expand this budget's linked bill schedules into the month's
        # occurrences so the burn-down steps + amount-matches them.
        bills = [
            bc.BillSchedule(
                category_id=d["category_id"], cadence=d["cadence"],
                anchor_date=d["anchor_date"], amount=d["amount"],
                end_date=d["end_date"],
            )
            # ADR-173: every scheduled outflow in the perimeter, not only the
            # ones someone linked to an envelope via "Make this a bill…". A
            # scheduled transaction is a known future spend either way, and the
            # link is an envelope-UI concept the projection has no business
            # depending on.
            for d in self._repo.list_perimeter_schedules(self._budget.id)
        ]
        occ = bc.bill_occurrences_in_month(bills, month)
        # Bucketing maps are now needed for both scopes (to classify bill vs
        # discretionary actuals), not just the single-category scope.
        parent_map = self._repo.category_parent_map()
        budgeted_ids = {
            r.category_id
            for s in self._matrix.sections for r in s.rows
            if not r.is_unbudgeted and r.category_id is not None
        }
        if self._scope_cat is None:
            exp = next(
                (s for s in self._matrix.sections if s.kind == "expense"), None
            )
            cell = exp.subtotal[mi] if exp else None
            data = bc.compute_burndown(
                perimeter_txns=ptxns, month=month,
                total_planned=_pacing_target(cell),
                parent_map=parent_map, budgeted_ids=budgeted_ids,
                kind_map=kind_map, scope_label="Whole budget",
                bill_occurrences=occ,
            )
        else:
            row = self._expense_row(self._scope_cat)
            cell = row.cells[mi] if row else None
            data = bc.compute_burndown(
                perimeter_txns=ptxns, month=month,
                total_planned=_pacing_target(cell),
                target_category_id=self._scope_cat,
                parent_map=parent_map,
                budgeted_ids=budgeted_ids, kind_map=kind_map,
                scope_label=row.label if row else "",
                bill_occurrences=occ,
            )
        self._chart.set_data(data)
        self._paint_verdict(data, cell)

    def _paint_verdict(self, data, cell) -> None:
        """The chart's headline — the answer, stated (ADR-172).

        ``projected_remaining`` and ``runs_out_day`` were the two things the
        reader was being asked to derive by eye from where a dashed line
        stopped. A chart that has computed the answer should say it.
        """
        ccy = self._matrix.display_ccy or ""
        if data is None or data.total_planned <= 0:
            self._verdict.clear()
            return
        left = data.projected_remaining
        if data.runs_out_day is not None:
            day = data.runs_out_day
            when = (
                "already" if day <= data.today_day
                else f"on {day} {_MONTH_ABBR[int(self._month[5:7])][:3]}"
            )
            text = (
                f"Over budget {when} &nbsp;·&nbsp; "
                f"{_money(ccy, abs(left))} over by month end"
                if left < 0 else
                f"Runs out {when} &nbsp;·&nbsp; the plan is fully spent"
            )
            colour = _bad_ink()
        else:
            text = (
                f"On track &nbsp;·&nbsp; {_money(ccy, left)} left on "
                f"{data.period_days} "
                f"{_MONTH_ABBR[int(self._month[5:7])][:3]}"
            )
            colour = _good_ink()
        # The buffer, if there is one. It is deliberately *not* the pacing
        # target (ADR-172) — you don't aim to spend a rollover surplus — but
        # it is the difference between "over budget" and "over budget with
        # nothing behind it", so it is worth a word.
        #
        # Measured as available − allocation, not from ``carry_in``: a section
        # subtotal sums its rows' *available* but hard-codes its own carry_in
        # to zero (compute_matrix step 4), so reading carry_in finds nothing on
        # the whole-budget scope — where the buffer is largest and most worth
        # saying. The subtraction is the definition anyway: the buffer is the
        # gap between what you can spend and what you planned to.
        note = ""
        buffer = (
            cell.available - cell.allocation if cell is not None else _ZERO
        )
        if buffer > 0:
            note = (
                f" &nbsp;<span style='color:{_muted_ink()}'>"
                f"(+{_money(ccy, buffer)} rolled over if needed)</span>"
            )
        self._verdict.setText(
            f"<b style='color:{colour}'>{text}</b>{note}"
        )

    def _expense_row(self, category_id: int) -> Optional[bc.MatrixRow]:
        for s in self._matrix.sections:
            for r in s.rows:
                if not r.is_unbudgeted and r.category_id == category_id:
                    return r
        return None

    # ── edit / drill ──

    def _edit_line(self, line_id: int, label: str, current: Decimal) -> None:
        val, ok = QInputDialog.getDouble(
            None, "Set budget",
            f"{label} — {_month_label(self._month)}:",
            float(current), 0.0, 1_000_000_000.0, 2,
        )
        if not ok:
            return
        # edit_cb runs the copy-forward prompt, writes, and triggers the
        # window's re-render (which calls set_data again) — no manual refresh.
        self._edit_cb(line_id, self._month, Decimal(str(val)))

    def _drill_row(self, row: bc.MatrixRow) -> None:
        if row.is_unbudgeted:
            self._drill_cb(
                "unbudgeted", None, row.kind, self._month,
                f"Unbudgeted {row.kind}",
            )
        elif row.is_group:
            # Match the bar: a group's fill is its whole subtree, so the drill
            # must cover the subtree too, not just the residual (ADR-170).
            self._drill_cb(
                "group", row.category_id, row.kind, self._month,
                f"{row.label} — all",
            )
        else:
            self._drill_cb(
                "line", row.category_id, row.kind, self._month, row.label,
            )


def _clear_layout(layout) -> None:
    """Empty a layout, *and* detach its widgets from the visible tree now.

    ``deleteLater`` alone is not enough. Taking a widget out of a layout does
    not unparent it — it stays a child of the list and keeps painting at its
    old geometry until the deferred delete is processed, so a rebuild draws the
    new rows *underneath the old ones* and the bottom of the list renders as
    overlapping text. ``setParent(None)`` removes it from the tree on the spot;
    ``deleteLater`` then frees it safely (never ``del``/immediate destruction —
    this can run from a signal handler on the very widget being cleared).
    """
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
