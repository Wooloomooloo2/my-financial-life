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
  ↻ glyph on rolling lines and a muted Unbudgeted row per section.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
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
_MUTED = "#64748b"     # slate-500
_GREEN_TXT = "#15803d"
_RED_TXT = "#b91c1c"
_ZERO = Decimal("0.00")


def _month_label(month: str) -> str:
    return f"{_MONTH_ABBR[int(month[5:7])]} {month[:4]}"


def _fmt(value: Decimal) -> str:
    return f"{value:,.2f}"


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

    def __init__(self, repo: Repository, *, edit_cb, drill_cb, parent=None):
        super().__init__(parent)
        self._repo = repo
        self._edit_cb = edit_cb       # (line_id, month, Decimal) -> bool
        self._drill_cb = drill_cb     # (mode, target_cat, kind, month, label)
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
        mf.setPointSize(11)
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

        # Burn-down block.
        from mfl_desktop.ui.burn_down_chart import BurnDownChart
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Burn-down:"))
        self._scope = QComboBox()
        self._scope.setMinimumWidth(200)
        self._scope.currentIndexChanged.connect(self._on_scope_changed)
        scope_row.addWidget(self._scope)
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

    def set_data(self, budget, matrix: bc.BudgetMatrix) -> None:
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
                if not row.is_unbudgeted and row.category_id is not None:
                    self._scope.addItem(row.label, row.category_id)
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

        ccy = self._matrix.display_ccy or ""
        pool = self._matrix.pool
        assigned = self._matrix.assigned_by_month[i]
        unalloc = pool - assigned
        colour = _RED_TXT if unalloc < 0 else _GREEN_TXT
        self._unalloc.setText(
            f"Pool: <b>{ccy} {_fmt(pool)}</b> &nbsp;·&nbsp; "
            f"Assigned: <b>{_fmt(assigned)}</b> &nbsp;·&nbsp; "
            f"Unallocated: <b style='color:{colour}'>{_fmt(unalloc)}</b>"
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
            self._list_lay.addWidget(self._section_header(section.title))
            for row in section.rows:
                self._list_lay.addWidget(self._envelope_row(row, mi))
            if len(section.rows) >= 2:
                self._list_lay.addWidget(self._subtotal_row(section, mi))
        self._list_lay.addStretch(1)

    def _section_header(self, title: str) -> QWidget:
        lbl = QLabel(title.upper())
        f = QFont()
        f.setBold(True)
        f.setPointSize(9)
        lbl.setFont(f)
        tokens.themed(lbl, "color:{muted_strong}; background:{surface_alt}; padding:4px 6px;")
        return lbl

    def _envelope_row(self, row: bc.MatrixRow, mi: int) -> QWidget:
        cell = row.cells[mi]
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 1, 6, 1)
        lay.setSpacing(8)

        # Name (+ ↻ for rolling lines).
        label = row.label
        if not row.is_unbudgeted and row.rollover == "accumulate":
            label += "  ↻"
        name = QLabel(label)
        name.setMinimumWidth(190)
        name.setMaximumWidth(190)
        name.setWordWrap(False)
        if row.is_unbudgeted:
            name.setStyleSheet(f"color:{_MUTED};")
        lay.addWidget(name)

        bar = _Bar()
        bar.doubleClicked.connect(
            lambda r=row: self._drill_row(r)
        )
        lay.addWidget(bar, stretch=1)

        if row.is_unbudgeted:
            bar.set_fill(1.0, over=False, muted=True)
            amt = QLabel(f"{_fmt(cell.actual)} spent")
            amt.setStyleSheet(f"color:{_MUTED};")
            amt.setMinimumWidth(150)
            amt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lay.addWidget(amt)
            lay.addSpacing(86)
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

        carry = cell.carry_in
        avail_txt = _fmt(available)
        if carry != 0:
            avail_txt += f" ({carry:+,.2f})"
        amt = _ClickLabel(f"{_fmt(actual)} / {avail_txt}")
        amt.setMinimumWidth(160)
        amt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        amt.setToolTip("Click to edit this month's budget")
        amt.clicked.connect(
            lambda lid=row.line_id, lbl=row.label, cur=cell.allocation:
            self._edit_line(lid, lbl, cur)
        )
        lay.addWidget(amt)

        diff = cell.diff
        dtxt = f"+{_fmt(diff)}" if diff > 0 else _fmt(diff)
        dl = QLabel(dtxt)
        dl.setMinimumWidth(78)
        dl.setMaximumWidth(78)
        dl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if diff < 0:
            dl.setStyleSheet(f"color:{_RED_TXT};")
        elif diff > 0:
            dl.setStyleSheet(f"color:{_GREEN_TXT};")
        lay.addWidget(dl)
        return w

    def _subtotal_row(self, section: bc.MatrixSection, mi: int) -> QWidget:
        cell = section.subtotal[mi]
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(8)
        name = QLabel(f"{section.title} — total")
        f = QFont()
        f.setBold(True)
        name.setFont(f)
        name.setMinimumWidth(190)
        lay.addWidget(name)
        lay.addStretch(1)
        amt = QLabel(f"{_fmt(cell.actual)} / {_fmt(cell.available)}")
        amt.setFont(f)
        amt.setMinimumWidth(160)
        amt.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(amt)
        dtxt = f"+{_fmt(cell.diff)}" if cell.diff > 0 else _fmt(cell.diff)
        dl = QLabel(dtxt)
        dl.setFont(f)
        dl.setMinimumWidth(78)
        dl.setMaximumWidth(78)
        dl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if cell.diff < 0:
            dl.setStyleSheet(f"color:{_RED_TXT};")
        elif cell.diff > 0:
            dl.setStyleSheet(f"color:{_GREEN_TXT};")
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
        if self._scope_cat is None:
            exp = next(
                (s for s in self._matrix.sections if s.kind == "expense"), None
            )
            total = exp.subtotal[mi].available if exp else _ZERO
            data = bc.compute_burndown(
                perimeter_txns=ptxns, month=month, total_planned=total,
                kind_map=kind_map, scope_label="Whole budget",
            )
        else:
            row = self._expense_row(self._scope_cat)
            total = row.cells[mi].available if row else _ZERO
            budgeted_ids = {
                r.category_id
                for s in self._matrix.sections for r in s.rows
                if not r.is_unbudgeted and r.category_id is not None
            }
            data = bc.compute_burndown(
                perimeter_txns=ptxns, month=month, total_planned=total,
                target_category_id=self._scope_cat,
                parent_map=self._repo.category_parent_map(),
                budgeted_ids=budgeted_ids, kind_map=kind_map,
                scope_label=row.label if row else "",
            )
        self._chart.set_data(data)

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
        else:
            self._drill_cb(
                "line", row.category_id, row.kind, self._month, row.label,
            )


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
