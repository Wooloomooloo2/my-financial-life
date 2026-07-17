"""The Budget screen (ADR-058) — the 12-month editable matrix.

A non-modal main window opened from Budget ▸ Open…. Layout (top-down):

- **Header strip:** budget picker (New / Duplicate / Rename / Delete), the
  period, and Setup… / Period… verbs, plus the soft Unallocated indicator
  (pool − assigned this month) per ADR-058 D2.
- **Matrix:** a `QTableView` over `BudgetMatrixModel`. Rows are the budgeted
  envelopes grouped into Income / Expenses / Transfers sections, each shown as
  three lines — **Budget / Actual / Diff** (principle 8) — across 12 month
  columns. Each section carries a synthetic **Unbudgeted** row (principle 9)
  and a subtotal. **Budget cells are editable** (principle 10): committing a
  value offers copy-forward (just this month / this + later / all months) per
  ADR-058 D1, then writes atomically and reloads.
- **The tree (ADR-170):** a budgeted category with budgeted descendants renders
  as a **group** — a roll-up header (own residual + every descendant, never
  editable), its children indented beneath, and the parent's own line labelled
  **'Everything else'** when it still holds money or spending. Sections and
  groups collapse by clicking their label; the set is persisted per budget in
  the file's `setting` table and shared with the monthly view. A collapsed
  group still shows its full roll-up, so collapsing costs no information.

**Per-line controls (R2):** a ↻ glyph in the label marks a line whose unspent
budget rolls forward, and right-clicking a line opens a menu to toggle that
rollover policy, change its role (expenses only), or remove it from the budget
— so the policy is both visible and editable without opening Setup. The
per-child actual breakdown stays on the existing double-click-Actual drill.

Refreshes on `WindowActivate` so flipping back from the register reflects new
actuals. Everything reads from `budget_calc.compute_matrix` — the single source
of budget truth.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop import budget_calc as bc
from mfl_desktop import goal_calc
from mfl_desktop.db.repository import (
    BUDGET_ROLES,
    AccountSummary,
    Budget,
    Repository,
)
from mfl_desktop.ui.budget_drilldown_window import BudgetDrillDownWindow
from mfl_desktop.ui.budget_monthly_view import BudgetMonthlyView
from mfl_desktop.ui.budget_setup_dialog import BudgetSetupDialog
from mfl_desktop.ui.goal_dialog import GoalDialog
from mfl_desktop.ui.page_header import PageHeader
from mfl_desktop.ui import tokens
from mfl_desktop.ui.chart_helpers import currency_symbol

_MONTH_ABBR = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

_ROLE_LABELS = {
    "bills": "Bills",
    "saving": "Saving",
    "discretionary": "Discretionary",
}

_ROLLOVER_GLYPH = "↻"   # marks a line whose unspent budget carries forward

# One tree level of indent in the label column (ADR-170). Spaces, not a
# delegate: the label column is plain text in a QTableView, and the whole point
# is a *slight* step — enough to read the nesting, not enough to shove a
# three-deep line off the column.
_INDENT = "    "

# Row kinds in the flattened model.
_SECTION = "section"
_METRIC = "metric"        # one of budget / actual / diff for a line
_SUBTOTAL = "subtotal"
_NET = "net"              # bottom-line Net (Income − Expenses) metric row
_NET_HEADER = "net_header"

_NET_TITLE = "Net (Income − Expenses)"

# Matrix colours are resolved from the design tokens at data()-time (not as
# module-level QColor singletons), so they follow the active light/dark theme
# (ADR-076). data() is re-queried on every repaint — including the global
# force-repaint theme.apply_theme does on a live toggle — so these stay correct
# with no per-model signal wiring. Each token's *light* value equals the hex it
# replaced, so light mode is unchanged.
def _over() -> QColor:        return QColor(tokens.c("negative_strong"))  # over budget / deficit
def _under() -> QColor:       return QColor(tokens.c("positive_strong"))  # under budget / surplus
def _section_bg() -> QColor:  return QColor(tokens.c("border"))           # section header rows
def _subtotal_bg() -> QColor: return QColor(tokens.c("surface_alt"))      # subtotal / net rows
def _today_bg() -> QColor:    return QColor(tokens.c("today_col"))        # today's month column
def _total_bg() -> QColor:    return QColor(tokens.c("canvas"))           # far-right Total column
def _rollover_bg() -> QColor: return QColor(tokens.c("rollover_bg"))      # Budget cell w/ carried-in rollover
def _muted() -> QColor:       return QColor(tokens.c("muted"))            # secondary label ink

_ZERO_D = Decimal("0.00")


def _fmt(value: Decimal) -> str:
    """'1,234.56' / '-1,234.56'. Currency symbol lives in the header, not in
    every cell, to keep the grid scannable."""
    return f"{value:,.2f}"


def _fmt_month(month: str) -> str:
    """'Jun 26' — the *column-header* format. Unambiguous only in context: the
    matrix's twelve columns are all one year, so the reader supplies the
    century. Do not use it for a date that can be decades out — see
    ``_fmt_month_long``."""
    y, m = int(month[:4]), int(month[5:7])
    return f"{_MONTH_ABBR[m]} {y % 100:02d}"


def _fmt_month_long(month: str) -> str:
    """'Jun 2049' — a four-digit year, for a date standing on its own.

    A goal's target date is the one place the app prints a month *decades*
    out, and it was borrowing the column-header format: a 2049 mortgage payoff
    rendered as "by Jun 49", which reads as 1949 (ADR-161).
    """
    y, m = int(month[:4]), int(month[5:7])
    return f"{_MONTH_ABBR[m]} {y}"


def _money(currency: str, value: Decimal) -> str:
    """'£822.64' / '-£2,387.36' — the symbol, not the ISO code.

    The budget window was the last surface printing money as ``GBP 822.64``;
    ``currency_symbol`` (ADR-159) is the single definition of the glyph and
    already falls back to a spaced code for currencies we have no symbol for.
    The sign goes *outside* the symbol — "-£20", never "£-20".
    """
    sign = "-" if value < 0 else ""
    return f"{sign}{currency_symbol(currency)}{_fmt(abs(value))}"


def _muted_label(text: str) -> QLabel:
    """A quiet inline caption ('Budget:', 'View:') — reads as a field label,
    not as content."""
    lbl = QLabel(text)
    tokens.themed(lbl, "color: {muted};")
    return lbl


# ── flattened row spec ─────────────────────────────────────────────────────


class _Row:
    __slots__ = ("kind", "section_idx", "matrix_row", "metric", "collapse_key")

    def __init__(
        self, kind, section_idx, matrix_row=None, metric=None,
        collapse_key=None,
    ):
        self.kind = kind
        self.section_idx = section_idx
        self.matrix_row = matrix_row   # bc.MatrixRow or bc.MatrixSection
        self.metric = metric           # 'budget' | 'actual' | 'diff'
        # ADR-170: set on rows that toggle a collapse — a section header or a
        # group's Budget row. None on everything else.
        self.collapse_key = collapse_key




class BudgetMatrixModel(QAbstractTableModel):
    """Flattens a BudgetMatrix into a (Budget/Actual/Diff)-per-line table.

    Column 0 is the row label; columns 1..N are the budget's months. Only
    Budget metric cells on real (non-Unbudgeted) lines are editable; committing
    one routes through ``edit_cb(line_id, month, amount) -> bool``.
    """

    def __init__(self, matrix: bc.BudgetMatrix, edit_cb, collapse_cb) -> None:
        super().__init__()
        self._m = matrix
        self._edit_cb = edit_cb
        # ADR-170: ``collapse_cb(key) -> None`` asks the window to flip and
        # persist a collapse key. The window owns the state (it has the budget
        # id and the repo); the model only renders it.
        self._collapse_cb = collapse_cb
        self._rows: list[_Row] = []
        self._today_col: Optional[int] = None
        self._collapsed: set[str] = set()   # collapse keys, from the window
        self._net_budget: list[Decimal] = []
        self._net_actual: list[Decimal] = []
        self._rebuild_rows()

    def set_matrix(
        self, matrix: bc.BudgetMatrix, collapsed: set[str],
    ) -> None:
        """Replace the model's data **in place** (one persistent model, reset
        around the swap). Reusing the model rather than ``setModel`` with a
        fresh one avoids orphaning an open inline editor — which Qt flags as
        'commitData called with an editor that does not belong to this view'
        and which, mid-event-handler on macOS, can tear the window down."""
        self.beginResetModel()
        self._m = matrix
        self._collapsed = set(collapsed)
        self._rebuild_rows()
        self.endResetModel()

    def _compute_net(self) -> None:
        """Per-month Net = Income − Expenses − Transfers, for the bottom line.
        Summing a Budget column across sections raw would be nonsense (income
        and expense have opposite intent), so the bottom row is the *net*."""
        n = len(self._m.months)
        nb = [_ZERO_D] * n
        na = [_ZERO_D] * n
        for s in self._m.sections:
            # Goals are transfer-like movements (cash → debt), not income or
            # expense — they integrate via `assigned`/Unallocated, not the Net
            # line — so keep them out of Income − Expenses − Transfers.
            if s.kind == "goals":
                continue
            sign = Decimal(1) if s.kind == "income" else Decimal(-1)
            for mi in range(n):
                nb[mi] += sign * s.subtotal[mi].allocation
                na[mi] += sign * s.subtotal[mi].actual
        self._net_budget, self._net_actual = nb, na

    def _rebuild_rows(self) -> None:
        self._compute_net()
        self._rows = []
        for si, section in enumerate(self._m.sections):
            skey = bc.section_key(section.kind)
            self._rows.append(_Row(_SECTION, si, section, collapse_key=skey))
            if skey in self._collapsed:
                continue
            for mr in bc.visible_rows(section.rows, self._collapsed):
                gkey = bc.row_group_key(mr)
                for metric in ("budget", "actual", "diff"):
                    self._rows.append(_Row(
                        _METRIC, si, mr, metric,
                        # Only the Budget row carries the chevron / click
                        # target — the Actual and Diff rows beneath it are
                        # continuations of the same line.
                        collapse_key=gkey if metric == "budget" else None,
                    ))
            # The subtotal is only meaningful when more than one row feeds it.
            # A section with a single row (e.g. only an Unbudgeted row, or one
            # budgeted line) would just duplicate that row and read as
            # double-counting — so skip it.
            #
            # ADR-170 counts *top-level* rows, not all rows: a section holding
            # one group with four children has five rows, but the group header
            # already rolls all of them up, so a subtotal beneath it would
            # restate the identical figure — exactly the duplication this rule
            # exists to prevent.
            if sum(1 for r in section.rows if r.depth == 0) >= 2:
                for metric in ("budget", "actual", "diff"):
                    self._rows.append(_Row(_SUBTOTAL, si, section, metric))
        # Bottom line: Net across all sections, per month + the Total column.
        if self._m.sections:
            self._rows.append(_Row(_NET_HEADER, -1))
            for metric in ("budget", "actual", "diff"):
                self._rows.append(_Row(_NET, -1, None, metric))
        if self._m.today_month in self._m.months:
            self._today_col = self._m.months.index(self._m.today_month) + 1

    # The far-right column index (after the label + N month columns).
    def _total_col(self) -> int:
        return 1 + len(self._m.months)

    # ── shape ──

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        # label + N months + Total
        return 0 if parent.isValid() else 2 + len(self._m.months)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal or role != Qt.DisplayRole:
            return None
        if section == 0:
            return "Category"
        if section == self._total_col():
            return "Total"
        return _fmt_month(self._m.months[section - 1])

    # ── data ──

    def _cell(self, mr, metric, month_idx) -> Decimal:
        # A section's per-month cells live on .subtotal; a line's on .cells.
        cells = mr.subtotal if isinstance(mr, bc.MatrixSection) else mr.cells
        c = cells[month_idx]
        if metric == "budget":
            return c.allocation
        if metric == "actual":
            return c.actual
        return c.diff

    def _total(self, mr, metric) -> Decimal:
        """A row's year sum for the far-right Total column."""
        cells = mr.subtotal if isinstance(mr, bc.MatrixSection) else mr.cells
        if metric == "budget":
            return sum((c.allocation for c in cells), _ZERO_D)
        if metric == "actual":
            return sum((c.actual for c in cells), _ZERO_D)
        return sum((c.diff for c in cells), _ZERO_D)

    def _net_value(self, metric, month_idx=None, total=False) -> Decimal:
        if total:
            nb = sum(self._net_budget, _ZERO_D)
            na = sum(self._net_actual, _ZERO_D)
        else:
            nb = self._net_budget[month_idx]
            na = self._net_actual[month_idx]
        if metric == "budget":
            return nb
        if metric == "actual":
            return na
        return na - nb   # diff: actual net − budget net (positive = better)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        total_col = self._total_col()
        is_total = col == total_col

        # Section / Net header rows.
        if row.kind in (_SECTION, _NET_HEADER):
            if col == 0:
                if role == Qt.DisplayRole:
                    if row.kind == _NET_HEADER:
                        return _NET_TITLE
                    # Chevron shows expand/collapse state (click toggles).
                    chevron = (
                        "▸  " if row.section_idx in self._collapsed else "▾  "
                    )
                    return chevron + row.matrix_row.title
                if role == Qt.BackgroundRole:
                    return _section_bg()
                if role == Qt.FontRole:
                    f = QFont()
                    f.setBold(True)
                    return f
                return None
            # Month / Total cells: a section header carries its **Budget total**
            # — the section's headline plan, and the only totals still visible
            # once the section is collapsed. (The Net header's own Budget /
            # Actual / Diff rows sit directly below it, so leave it blank.)
            if row.kind == _SECTION:
                section = row.matrix_row
                val = (
                    sum((c.allocation for c in section.subtotal), _ZERO_D)
                    if is_total else section.subtotal[col - 1].allocation
                )
                if role == Qt.DisplayRole:
                    return _fmt(val)
                if role == Qt.TextAlignmentRole:
                    return int(Qt.AlignRight | Qt.AlignVCenter)
                if role == Qt.FontRole:
                    f = QFont()
                    f.setBold(True)
                    return f
            if role == Qt.BackgroundRole:
                return _section_bg()
            return None

        is_sub = row.kind == _SUBTOTAL
        is_net = row.kind == _NET
        is_summary = is_sub or is_net
        mr = row.matrix_row
        metric = row.metric

        # Label column.
        if col == 0:
            if role == Qt.DisplayRole:
                if is_net:
                    return {"budget": "  Budget", "actual": "  Actual",
                            "diff": "  Diff"}[metric]
                if is_sub:
                    return {"budget": f"{mr.title} — Budget",
                            "actual": "  Actual",
                            "diff": "  Diff"}[metric]
                # ADR-170: indent by tree depth. The Actual / Diff rows are
                # continuations of their Budget row, so they indent with it —
                # otherwise a nested line's own metrics would read as belonging
                # to whatever sits at the outer level.
                pad = _INDENT * mr.depth
                if metric == "budget":
                    label = mr.label
                    # A ↻ marks a line whose unspent budget rolls forward, so
                    # the policy is readable at a glance (toggle via right-click).
                    # Only on rows that *own* a policy: a group header is a
                    # roll-up of lines that each carry their own, so a glyph
                    # there would claim a policy the header doesn't have.
                    if mr.is_editable and mr.rollover == "accumulate":
                        label = f"{label}  {_ROLLOVER_GLYPH}"
                    if mr.is_group:
                        chevron = (
                            "▸ " if row.collapse_key in self._collapsed
                            else "▾ "
                        )
                        return f"{pad}{chevron}{label}"
                    return f"{pad}{label}"
                sub_pad = pad + "  "
                return sub_pad + ("Actual" if metric == "actual" else "Diff")
            if role == Qt.ToolTipRole and metric == "budget" and not is_summary:
                if mr.is_group:
                    return (
                        "The total for this group — its own ‘Everything else’ "
                        "plus every budgeted line beneath it.\nClick to "
                        "collapse; the total stays visible either way."
                    )
                if mr.row_kind == "residual":
                    return (
                        "Spending under this group that no budgeted line "
                        "below it claims.\nBudget it here, or add lines to "
                        "itemise it further."
                    )
                if mr.is_unbudgeted:
                    return None
                if mr.rollover == "accumulate":
                    return (
                        "Unspent budget rolls over to the next month (↻).\n"
                        "Right-click to change the rollover or role."
                    )
                return (
                    "Resets each month — unspent budget does not carry "
                    "forward.\nRight-click to enable rollover or change role."
                )
            if role == Qt.ForegroundRole and metric != "budget" and not is_net:
                return _muted()
            if role == Qt.BackgroundRole and is_summary:
                return _subtotal_bg()
            if role == Qt.FontRole and (is_summary or metric == "budget"):
                f = QFont()
                if is_summary:
                    f.setBold(True)
                else:
                    # Bold marks a line you can plan against, or a group's
                    # roll-up. 'Everything else' is a remainder — it reads
                    # quieter than the lines it is left over from.
                    f.setBold(
                        mr.is_group
                        or (mr.is_editable and mr.row_kind != "residual")
                    )
                return f
            return None

        month_idx = None if is_total else col - 1

        # Value for this cell — net rows, the Total column, or a normal cell.
        if is_net:
            value = self._net_value(metric, month_idx, total=is_total)
        elif is_total:
            value = self._total(mr, metric)
        else:
            value = self._cell(mr, metric, month_idx)

        # The Unbudgeted row has no budget — show a dash, not an editable 0.
        if not is_summary and metric == "budget" and mr.is_unbudgeted:
            if role == Qt.DisplayRole:
                return "—"
            if role == Qt.TextAlignmentRole:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if role == Qt.ForegroundRole:
                return _muted()
            if role == Qt.BackgroundRole and is_total:
                return _total_bg()
            return None

        # Rolled-over carry on a (month) Budget cell. Drives the cell's yellow
        # background (_rollover_bg) and its reconciliation tooltip — Budget /
        # Actual / Diff still reconcile via available = alloc + carry. It is
        # *not* rendered inline: the "(+205.00)" annotation overflowed the
        # fixed 86px month column and was elided away, hiding the budget figure
        # itself (ADR-124). The yellow background + tooltip + the ↻ label glyph
        # signal the rollover without clipping the number.
        carry = (
            mr.cells[month_idx].carry_in
            if (row.kind == _METRIC and metric == "budget" and not is_total)
            else _ZERO_D
        )

        if role == Qt.DisplayRole:
            if metric == "diff" and value > 0:
                return f"+{_fmt(value)}"
            return _fmt(value)
        if role == Qt.EditRole and self._editable(row, col):
            return _fmt(value)
        if role == Qt.ToolTipRole and carry != 0:
            avail = mr.cells[month_idx].available
            if carry > 0:
                return (
                    f"Budgeted {_fmt(value)} + {_fmt(carry)} rolled over "
                    f"= {_fmt(avail)} available this month"
                )
            return (
                f"Budgeted {_fmt(value)} − {_fmt(abs(carry))} overspend "
                f"carried in = {_fmt(avail)} available this month"
            )
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole and metric == "diff":
            if value < 0:
                return _over()
            if value > 0:
                return _under()
        if role == Qt.BackgroundRole:
            if carry != 0:
                return _rollover_bg()
            if is_summary:
                return _subtotal_bg()
            if is_total:
                return _total_bg()
            if col == self._today_col:
                return _today_bg()
        if role == Qt.FontRole and (is_summary or is_total):
            f = QFont()
            f.setBold(True)
            return f
        return None

    # ── editing ──

    def _editable(self, row: _Row, col: int) -> bool:
        return (
            row.kind == _METRIC
            and row.metric == "budget"
            # ADR-170: leaves and 'Everything else' hold a real stored
            # allocation; a group header is a computed roll-up — typing into it
            # would have no line to write to. Unbudgeted has no line either.
            and row.matrix_row.is_editable
            and row.matrix_row.kind != "goals"   # goal amounts are computed
            and 1 <= col <= len(self._m.months)   # months only — not Total
        )

    def flags(self, index):
        base = super().flags(index)
        if index.isValid() and self._editable(
            self._rows[index.row()], index.column()
        ):
            return base | Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole) -> bool:
        if role != Qt.EditRole or not index.isValid():
            return False
        row = self._rows[index.row()]
        if not self._editable(row, index.column()):
            return False
        try:
            amount = Decimal(str(value).replace(",", "").strip() or "0")
        except (InvalidOperation, ValueError):
            return False
        if amount < 0:
            return False
        month = self._m.months[index.column() - 1]
        return bool(self._edit_cb(row.matrix_row.line_id, month, amount))


# Goal-card state → design token (resolved at paint/render time for theme
# correctness, ADR-076). in-progress = accent, met = positive_strong,
# overdue = negative_strong; the bar track is the border tone.
_GOAL_TOKEN = {
    "blue": "accent",            # in-progress
    "green": "positive_strong",  # met
    "red": "negative_strong",    # overdue, not met
}


class _GoalBar(QWidget):
    """A thin flat progress bar for a goal card — paintEvent to match the app's
    chart idiom (ADR-026). Fill colour reflects state: green met, red overdue,
    blue in-progress."""

    def __init__(self, pct: float, colour_token: str, parent=None) -> None:
        super().__init__(parent)
        self._pct = max(0.0, min(100.0, pct))
        self._colour_token = colour_token
        self.setFixedHeight(8)
        self.setMinimumWidth(120)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        radius = h / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(tokens.c("border")))
        p.drawRoundedRect(0, 0, w, h, radius, radius)
        fill_w = int(round(w * self._pct / 100.0))
        if fill_w > 0:
            p.setBrush(QColor(tokens.c(self._colour_token)))
            p.drawRoundedRect(0, 0, max(fill_w, h), h, radius, radius)
        p.end()


class _GoalCard(QFrame):
    """One goal as a compact clickable card: account, target line, required
    monthly, and a progress bar. Clicking opens the edit dialog."""

    def __init__(self, prog: "goal_calc.GoalProgress", on_edit, parent=None) -> None:
        super().__init__(parent)
        self._goal_id = prog.goal_id
        self._on_edit = on_edit
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        tokens.themed(self, "_GoalCard { border: 1px solid {border}; border-radius: 10px; background: {surface}; } _GoalCard:hover { border-color: {accent}; }")
        # Roomier than the original 208×(10,6,10,8): the card packs five lines
        # plus a progress bar, and at the old size the text ran to the edges and
        # collided with the neighbouring "+ Goal…" button (ADR-161).
        self.setFixedWidth(232)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(3)

        title = QLabel(prog.name)
        tf = title.font()
        tf.setBold(True)
        title.setFont(tf)
        lay.addWidget(title)

        n = prog.account_count
        meta = f"{n} account{'' if n == 1 else 's'} · {prog.currency}"
        if prog.rate_missing:
            meta += "  *"
        sub_meta = QLabel(meta)
        tokens.themed(sub_meta, "color: {subtle}; font-size: 11px;")  # slate-400
        if prog.rate_missing:
            sub_meta.setToolTip(
                "An account couldn't be converted to the goal currency "
                "(no exchange rate) and was left out of the totals."
            )
        lay.addWidget(sub_meta)

        if prog.is_met:
            state = "Paid off ✓" if prog.kind == "paydown" else "Reached ✓"
            colour_token = _GOAL_TOKEN["green"]
        elif prog.is_overdue:
            state = "Overdue"
            colour_token = _GOAL_TOKEN["red"]
        else:
            colour_token = _GOAL_TOKEN["blue"]
            target_mag = abs(prog.target_amount)
            state = (
                f"to {_money(prog.currency, target_mag)} "
                f"by {_fmt_month_long(prog.target_date[:7])}"
            )
        sub = QLabel(state)
        tokens.themed(sub, "color: {%s};" % colour_token)
        lay.addWidget(sub)

        if not prog.is_met:
            need = QLabel(
                f"Need {_money(prog.currency, prog.required_monthly)}/mo"
            )
            tokens.themed(need, "color: {muted_strong};")  # slate-600
            lay.addWidget(need)

        lay.addWidget(_GoalBar(prog.progress_pct, colour_token))
        pct = QLabel(f"{prog.progress_pct:.0f}% paid"
                     if prog.kind == "paydown"
                     else f"{prog.progress_pct:.0f}% saved")
        tokens.themed(pct, "color: {muted}; font-size: 11px;")  # slate-500
        lay.addWidget(pct)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._on_edit(self._goal_id)
        super().mouseReleaseEvent(event)


class BudgetWindow(QMainWindow):
    """The budget matrix window. Singleton per RegisterWindow."""

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Budget")
        self.resize(1180, 720)

        self._budget: Optional[Budget] = None
        self._matrix: Optional[bc.BudgetMatrix] = None
        self._drill_wins: list = []
        self._rendering = False
        # ADR-170: collapsed section / group keys for the current budget,
        # loaded per-budget from the file's setting table.
        self._collapsed: set[str] = set()
        # Cached numbers behind the rich-text Pool/Assigned/Unallocated line, so
        # a theme toggle can re-colour it without a full re-render (_paint_info).
        self._info_state: Optional[tuple] = None

        central = QWidget()
        central.setObjectName("budgetRoot")
        tokens.themed(central, "QWidget#budgetRoot { background-color: {canvas}; }")
        shell = QVBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        # The shared page header (ADR-119) — this window predated it and opened
        # straight onto a toolbar with no title at all.
        self._header = PageHeader(show_rule=True)
        self._header.set_heading("Budget", "")
        # The Annual ↔ Monthly toggle (R3) is a page-level mode switch, so it
        # belongs in the header's action slot, not buried in the verb row.
        self._header.add_action(_muted_label("View:"))
        self._view = QComboBox()
        self._view.addItem("Annual", 0)
        self._view.addItem("Monthly", 1)
        self._view.currentIndexChanged.connect(self._on_view_changed)
        self._header.add_action(self._view)
        shell.addWidget(self._header)

        root = QVBoxLayout()
        root.setContentsMargins(20, 10, 20, 14)
        root.setSpacing(8)
        body = QWidget()
        body.setLayout(root)
        shell.addWidget(body, stretch=1)

        # Toolbar: the budget picker, the one primary verb, and everything else
        # behind a single Manage menu (ADR-161). This row used to be six
        # identical grey buttons — New… Duplicate… Rename… Delete Period…
        # Set up… — with no hierarchy, which is most of why the screen read as
        # unfinished. Creating a budget is the call to action; the other five
        # are occasional management, and Delete is destructive enough that it
        # should not sit one stray click from Duplicate.
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(_muted_label("Budget:"))
        self._picker = QComboBox()
        self._picker.setMinimumWidth(220)
        self._picker.currentIndexChanged.connect(self._on_pick_budget)
        header.addWidget(self._picker)

        new_btn = QPushButton("+  New…")
        new_btn.setProperty("mflVariant", "primary")
        new_btn.clicked.connect(self._on_new)
        header.addWidget(new_btn)

        self._manage_btn = QPushButton("Manage")
        manage_menu = QMenu(self._manage_btn)
        for label, slot in (
            ("Set up…", self._on_setup),
            ("Period…", self._on_period),
            ("Duplicate…", self._on_duplicate),
            ("Rename…", self._on_rename),
        ):
            manage_menu.addAction(label, slot)
        manage_menu.addSeparator()
        manage_menu.addAction("Delete budget…", self._on_delete)
        self._manage_btn.setMenu(manage_menu)
        header.addWidget(self._manage_btn)

        header.addStretch(1)
        root.addLayout(header)

        # Soft Unallocated indicator + missing-rate banner.
        self._info_label = QLabel("")
        self._info_label.setTextFormat(Qt.RichText)
        root.addWidget(self._info_label)

        # Goals strip (R4b) — sits above the view stack so it shows in both the
        # Annual and Monthly views. Populated per-render from the budget's goals.
        self._goals_bar = QWidget()
        self._goals_layout = QHBoxLayout(self._goals_bar)
        self._goals_layout.setContentsMargins(0, 2, 0, 4)
        self._goals_layout.setSpacing(8)
        root.addWidget(self._goals_bar)

        # The matrix.
        self._table = QTableView()
        self._table.setAlternatingRowColors(False)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Interactive
        )
        self._table.verticalHeader().setVisible(False)
        # Double-click an Actual cell to drill into its transactions (ADR-058).
        self._table.doubleClicked.connect(self._on_cell_double_clicked)
        # Single-click a section header to expand/collapse it.
        self._table.clicked.connect(self._on_cell_clicked)
        # Right-click a budget line to toggle rollover / role / remove (R2).
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)

        # Monthly view (R3) — same matrix, one month in focus. Edits + drills
        # route back through this window so there's a single path for each.
        self._monthly = BudgetMonthlyView(
            self._repo, edit_cb=self._on_edit_allocation, drill_cb=self._drill,
            collapse_cb=self._toggle_collapse,
        )

        # The annual matrix (page 0) and monthly view (page 1) share the
        # central area; the View toggle flips between them.
        self._stack = QStackedWidget()
        self._stack.addWidget(self._table)
        self._stack.addWidget(self._monthly)
        root.addWidget(self._stack, stretch=1)

        self._empty_label = QLabel(
            "No budget yet. Click “+ New…” to create one, then "
            "“Manage ▸ Set up…” to choose accounts and categories."
        )
        self._empty_label.setAlignment(Qt.AlignCenter)
        tokens.themed(self._empty_label, "color: {muted};")
        root.addWidget(self._empty_label)

        self.setCentralWidget(central)
        # The info line's colours are baked into HTML, so they can't ride the
        # stylesheet re-apply — repaint them on a theme toggle instead.
        tokens.notifier.changed.connect(self._paint_info)
        self._reload_budget_list(select_id=None)

    # ── budget list / picker ──

    def _reload_budget_list(self, select_id: Optional[int]) -> None:
        budgets = self._repo.list_budgets()
        self._picker.blockSignals(True)
        self._picker.clear()
        for b in budgets:
            self._picker.addItem(b.name, b.id)
        self._picker.blockSignals(False)
        if not budgets:
            self._budget = None
            self._render()
            return
        idx = 0
        if select_id is not None:
            for i, b in enumerate(budgets):
                if b.id == select_id:
                    idx = i
                    break
        self._picker.setCurrentIndex(idx)
        self._on_pick_budget()

    def _on_pick_budget(self) -> None:
        bid = self._picker.currentData()
        self._budget = self._repo.get_budget(bid) if bid is not None else None
        # Collapse state is per-budget, so it reloads with the budget (ADR-170).
        self._collapsed = self._load_collapsed()
        self._render()

    def _on_view_changed(self) -> None:
        """Flip between the annual matrix and the monthly progress view."""
        self._stack.setCurrentIndex(self._view.currentData() or 0)
        self._sync_info_visibility()

    def _sync_info_visibility(self) -> None:
        """Only one Pool / Assigned / Unallocated line on screen (ADR-171).

        Both pages carry one, and on the Monthly page they were drawn a
        centimetre apart: the window's (fixed to *today's* month) and the
        monthly view's own (following its month selector). Two lines showing
        the same three labels with different numbers is worse than either —
        it reads as a contradiction until you work out that one of them is
        pinned to a month you aren't looking at.

        The monthly view's is the right one there — it is beside the selector
        it tracks — so the window's steps aside for it.
        """
        self._info_label.setVisible(self._stack.currentIndex() == 0)

    # ── render ──

    def _display_ccy(self) -> str:
        if self._budget and self._budget.currency:
            return self._budget.currency
        return self._repo.get_setting("base_currency") or "GBP"

    def _render(self) -> None:
        # Re-entrancy guard: building the model swaps the table's model, and a
        # second _render() interleaved with the first (e.g. a WindowActivate
        # arriving mid-rebuild) would free the model under construction.
        if self._rendering:
            return
        self._rendering = True
        try:
            self._render_inner()
        except Exception:  # noqa: BLE001
            # A refresh must NEVER crash — it's called from button slots, the
            # activate path, drill-downs, etc. An exception escaping any of
            # those tears the window down on macOS. Log it (so the real cause
            # is visible) and keep the window alive with its last good view.
            import traceback
            traceback.print_exc()
        finally:
            self._rendering = False

    def _render_inner(self) -> None:
        # The repo is shared with the register window; on app shutdown its
        # closeEvent closes the connection (ADR-057) while a queued
        # WindowActivate can still fire a refresh. Operating on a closed
        # connection raises, and an exception escaping a Qt event override can
        # tear the window down — so bail out cleanly if it's gone.
        if not self._repo.is_open():
            return
        if self._budget is None:
            self._stack.setVisible(False)
            self._view.setEnabled(False)
            self._empty_label.setVisible(True)
            self._info_label.setText("")
            self._goals_bar.setVisible(False)
            return
        self._stack.setVisible(True)
        self._view.setEnabled(True)
        self._empty_label.setVisible(False)

        budget = self._budget
        months = budget.months()
        ccy = self._display_ccy()
        today = date.today()
        today_month = today.strftime("%Y-%m")
        lines = self._repo.list_budget_lines(budget.id)
        allocations = self._repo.list_budget_allocations(budget.id)
        ptxns = self._repo.list_perimeter_txns(
            budget.id, months[0] + "-01", months[-1] + "-31",
        )
        pool, excluded = self._repo.compute_perimeter_pool(
            budget.id, display_ccy=ccy, on_date=today.isoformat(),
        )
        goal_plans = self._build_goal_plans(budget, months, today)
        matrix = bc.compute_matrix(
            budget=budget, lines=lines, allocations=allocations,
            perimeter_txns=ptxns, parent_map=self._repo.category_parent_map(),
            kind_map=self._repo.category_kind_map(), pool=pool,
            excluded_accounts=excluded, display_ccy=ccy,
            today_month=today_month, goal_plans=goal_plans,
        )

        self._matrix = matrix
        model = self._table.model()
        if isinstance(model, BudgetMatrixModel):
            # Update the existing model in place — no model swap under any open
            # editor (see BudgetMatrixModel.set_matrix).
            #
            # A reset clears the current index, so every edit (and every
            # WindowActivate refresh) dropped the reader's place in the grid —
            # see _restore_current_cell (ADR-170).
            cur = self._table.currentIndex()
            cur_rc = (cur.row(), cur.column()) if cur.isValid() else None
            model.set_matrix(matrix, self._collapsed)
            self._restore_current_cell(cur_rc)
        else:
            model = BudgetMatrixModel(
                matrix, self._on_edit_allocation, self._toggle_collapse,
            )
            model.set_matrix(matrix, self._collapsed)
            self._table.setModel(model)
        self._table.setColumnWidth(0, 240)
        for c in range(1, model.columnCount()):
            self._table.setColumnWidth(c, 86)
        # The Total column holds year-sized sums — give it a little more room.
        self._table.setColumnWidth(model.columnCount() - 1, 104)

        # Feed the same matrix to the monthly view (R3) so both pages stay in
        # lock-step off one computation — the single source of budget truth.
        # The collapse set rides along so the rebuild this triggers already has
        # it (ADR-170) — the two views share one set.
        self._monthly.set_data(budget, matrix, self._collapsed)

        # Pay-down / savings goals strip (R4b).
        self._render_goals(budget, today)

        # Soft Unallocated indicator for the focused (today's) month, else first.
        focus_idx = months.index(today_month) if today_month in months else 0
        assigned = matrix.assigned_by_month[focus_idx]
        unalloc = pool - assigned
        self._info_state = (
            ccy, pool, assigned, unalloc, _fmt_month(months[focus_idx]),
            tuple(excluded),
        )
        self._paint_info()
        self._sync_info_visibility()

        self._header.set_heading(
            "Budget", f"{budget.name} · {_fmt_month(months[0])} – {_fmt_month(months[-1])}"
        )
        self.setWindowTitle(f"Budget — {budget.name}")

    def _restore_current_cell(self, cur_rc) -> None:
        """Put the cursor back on the cell the reader was working in (ADR-170).

        ``beginResetModel`` **clears the current index**, and `_render` runs
        after every edit and every WindowActivate. So committing a budget
        amount dropped the selection: the cell you just typed into stopped
        being current, the highlight vanished, and arrowing or tabbing on to
        the next month restarted from nowhere. Re-find your place by eye, every
        single edit.

        Restoring the index also brings the cell back into view (Qt scrolls to
        the current index), which is what makes this read as 'the screen kept
        my place'. The scrollbar itself needs no help — a QTableView holds its
        value across a reset; it is only the cursor that is lost.

        Re-applied on the next event-loop turn as well: the view re-lays-out
        its geometry in a queued pass after the reset, and doing it once more
        afterwards outlives that — the same singleShot(0) idiom the activate
        refresh already uses.
        """
        if cur_rc is None:
            return

        def apply() -> None:
            model = self._table.model()
            if model is None:
                return
            row, col = cur_rc
            # A refresh can shorten the table (a line removed, a group
            # collapsed, a residual zeroed away) — don't restore off the end.
            if row < model.rowCount() and col < model.columnCount():
                self._table.setCurrentIndex(model.index(row, col))

        apply()
        QTimer.singleShot(0, apply)

    def _paint_info(self) -> None:
        """Render the Pool / Assigned / Unallocated line from the cached numbers.

        Split out of ``_render`` and hung off ``tokens.notifier.changed``
        because this label is *rich text*: its colours live inside an HTML
        string, not a stylesheet, so ``tokens.themed`` can't reach them and the
        ADR-097 dark-mode sweep missed it — it carried three frozen light-theme
        hexes. Resolving them here means they follow a live theme toggle, which
        a one-shot render would not (ADR-161).
        """
        if not self._info_state:
            self._info_label.clear()
            return
        ccy, pool, assigned, unalloc, focus_label, excluded = self._info_state
        colour = tokens.c("negative_strong" if unalloc < 0 else "positive_strong")
        info = (
            f"Pool: <b>{_money(ccy, pool)}</b> &nbsp;·&nbsp; "
            f"Assigned ({focus_label}): <b>{_money(ccy, assigned)}</b> &nbsp;·&nbsp; "
            f"Unallocated: <b style='color:{colour}'>{_money(ccy, unalloc)}</b>"
        )
        if excluded:
            # Exclusions carry their reason (a currency with no FX rate to the
            # display currency) — see Repository.compute_perimeter_pool
            # (ADR-058 R4a / ADR-138).
            info += (
                f" &nbsp;—&nbsp; <span style='color:{tokens.c('warning')}'>"
                f"{len(excluded)} account(s) excluded from the pool: "
                f"{', '.join(excluded)}</span>"
            )
        self._info_label.setText(info)

    # ── goals (R4b) ──

    def _build_goal_plans(
        self, budget: Budget, months: list[str], today: date,
    ) -> list[bc.GoalPlan]:
        """Turn each goal into per-month planned (required contribution spread
        from this month to the target month) + actual (contributions made), for
        the matrix Goals section. The required figure comes from `goal_calc` so
        the strip and the matrix agree."""
        goals = self._repo.list_budget_goals(budget.id)
        if not goals:
            return []
        aggs = self._repo.compute_goal_aggregates(
            budget.id, on_date=today.isoformat(),
        )
        cur_month = today.strftime("%Y-%m")
        plans: list[bc.GoalPlan] = []
        for g in goals:
            agg = aggs.get(g.id)
            if agg is None:
                continue
            prog = goal_calc.compute_goal_progress(
                g, start_amount=agg.start, current_amount=agg.current,
                today=today, rate_missing=bool(agg.excluded),
            )
            tgt_month = g.target_date[:7]
            planned: dict[str, Decimal] = {}
            if not prog.is_met:
                for m in months:
                    if cur_month <= m <= tgt_month:
                        planned[m] = prog.required_monthly
                # Overdue (target already past): the whole remainder is due now.
                if prog.is_overdue and cur_month in months:
                    planned[cur_month] = prog.required_monthly
            plans.append(bc.GoalPlan(
                goal_id=g.id, label=g.name,
                planned=planned, actual=self._goal_actual_by_month(g, months),
            ))
        return plans

    def _goal_actual_by_month(
        self, goal, months: list[str],
    ) -> dict[str, Decimal]:
        """Σ over the goal's accounts of (monthly inflow × share), each converted
        to the goal currency (ADR-058 R4c). Inflows are deposits/payments into
        each contributing account; a month with no convertible contribution is
        simply absent."""
        accounts = {a.id: a for a in self._repo.list_accounts()}
        out: dict[str, Decimal] = {}
        for link in goal.accounts:
            acc = accounts.get(link.account_id)
            if acc is None:
                continue
            share = Decimal(link.share_bp) / Decimal(10000)
            inflows = self._repo.account_inflows_by_month(
                link.account_id, months[0], months[-1],
            )
            for m, amt in inflows.items():
                conv, _ = self._repo.convert_amount(
                    amt * share, from_ccy=acc.currency, to_ccy=goal.currency,
                    on_date=f"{m}-28",
                )
                if conv is None:
                    continue
                out[m] = out.get(m, Decimal("0.00")) + conv
        return out

    def _render_goals(self, budget: Budget, today: date) -> None:
        """Rebuild the goals strip: one card per goal + an Add button. Cards are
        computed off live balances (balance-based progress, ADR-058 R4b/R4c)."""
        self._clear_goals_bar()
        self._goals_bar.setVisible(True)

        goals = self._repo.list_budget_goals(budget.id)
        aggs = self._repo.compute_goal_aggregates(
            budget.id, on_date=today.isoformat(),
        )

        self._goals_layout.addWidget(QLabel("Goals:"))
        for g in goals:
            agg = aggs.get(g.id)
            if agg is None:
                continue
            prog = goal_calc.compute_goal_progress(
                g, start_amount=agg.start, current_amount=agg.current,
                today=today, rate_missing=bool(agg.excluded),
            )
            self._goals_layout.addWidget(_GoalCard(prog, self._on_edit_goal))
        add = QPushButton("＋ Goal…")
        add.setToolTip("Set a savings or pay-down goal in this budget")
        add.clicked.connect(self._on_add_goal)
        self._goals_layout.addWidget(add)
        self._goals_layout.addStretch(1)

    def _clear_goals_bar(self) -> None:
        # setParent(None) before deleteLater: taking a widget out of a layout
        # doesn't unparent it, so without this the old goal cards keep painting
        # over the new ones until the deferred delete lands (ADR-171).
        while self._goals_layout.count():
            item = self._goals_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _goal_candidate_accounts(self, budget: Budget) -> list[AccountSummary]:
        """Accounts eligible for a goal: assets in the cash/investment families
        (to save toward) or liabilities (to pay down). The dialog filters by the
        chosen kind; an account can join several goals, split by share, so
        there's no 'already taken' exclusion (ADR-058 R4c).

        Goal accounts are deliberately **not** restricted to the budget perimeter
        — a savings/investment/pension vehicle you fund is usually outside your
        day-to-day spending perimeter (whose membership drives the pool +
        actuals), yet it's exactly what a savings goal targets."""
        return [
            a for a in self._repo.list_accounts()
            if a.is_liability or a.family in ("cash", "investment")
        ]

    def _goal_share_used(
        self, budget: Budget, exclude_goal_id: Optional[int] = None,
    ) -> dict[int, int]:
        """``account_id -> total share_bp`` already committed across the budget's
        goals (optionally excluding one goal, for edit mode) — feeds the dialog's
        >100% over-commit warning."""
        used: dict[int, int] = {}
        for g in self._repo.list_budget_goals(budget.id):
            if exclude_goal_id is not None and g.id == exclude_goal_id:
                continue
            for link in g.accounts:
                used[link.account_id] = used.get(link.account_id, 0) + link.share_bp
        return used

    def _on_add_goal(self) -> None:
        if self._budget is None:
            return
        candidates = self._goal_candidate_accounts(self._budget)
        if not candidates:
            QMessageBox.information(
                None, "No account to set a goal on",
                "A goal needs a savings/investment account (to save toward) or a "
                "credit/debt account (to pay down) in this budget's accounts."
                "\n\nAdd one via Set up… ▸ Accounts first.",
            )
            return
        dlg = GoalDialog(
            candidates=candidates,
            currencies=self._repo.list_distinct_currencies(),
            base_currency=self._display_ccy(),
            share_used=self._goal_share_used(self._budget),
            parent=None,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        self._repo.add_budget_goal(
            budget_id=self._budget.id,
            name=dlg.result_name(),
            kind=dlg.result_kind(),
            currency=dlg.result_currency(),
            target_amount=dlg.result_target_amount(),
            target_date=dlg.result_target_date(),
            accounts=dlg.result_accounts(),
            today=date.today().isoformat(),
        )
        self._render()

    def _on_edit_goal(self, goal_id: int) -> None:
        if self._budget is None:
            return
        goal = next(
            (g for g in self._repo.list_budget_goals(self._budget.id)
             if g.id == goal_id),
            None,
        )
        if goal is None:
            return
        dlg = GoalDialog(
            candidates=self._goal_candidate_accounts(self._budget),
            currencies=self._repo.list_distinct_currencies(),
            base_currency=self._display_ccy(),
            share_used=self._goal_share_used(
                self._budget, exclude_goal_id=goal_id,
            ),
            goal=goal,
            parent=None,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.was_deleted():
            self._repo.delete_budget_goal(goal_id)
        else:
            self._repo.update_budget_goal(
                goal_id,
                name=dlg.result_name(),
                target_amount=dlg.result_target_amount(),
                target_date=dlg.result_target_date(),
                currency=dlg.result_currency(),
                accounts=dlg.result_accounts(),
                today=date.today().isoformat(),
            )
        self._render()

    # ── edit (copy-forward) ──

    def _on_edit_allocation(
        self, line_id: int, month: str, amount: Decimal,
    ) -> bool:
        # parent=None on the prompt too — same cascade-close avoidance (ADR-058).
        scope = _ask_copy_forward_scope(None, _fmt_month(month))
        if scope is None:
            return False
        try:
            self._repo.set_line_allocation(line_id, month, amount, scope=scope)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not set amount", str(e))
            return False
        self._render()
        return True

    # ── drill-down (double-click an Actual) ──

    def _on_cell_clicked(self, index) -> None:
        """Single-click a section header or a group's Budget row → collapse it.

        ADR-170: only the label column toggles. On a section header the whole
        row is inert, but a group's Budget row has *editable month cells* — a
        click there is the start of an edit, and collapsing the row out from
        under it would be hostile.
        """
        model = self._table.model()
        if not isinstance(model, BudgetMatrixModel) or not index.isValid():
            return
        row = model._rows[index.row()]
        if row.collapse_key is None:
            return
        if row.kind == _METRIC and index.column() != 0:
            return
        self._toggle_collapse(row.collapse_key)

    # ── collapse state (ADR-170) ──

    def _collapse_setting_key(self) -> str:
        return "budget/collapsed"

    def _load_collapsed(self) -> set[str]:
        """The remembered collapse keys for the current budget.

        Per-file (the ``setting`` table, ADR-092) rather than app-level
        QSettings, for the same reason ADR-168 gave: the keys embed *this
        file's* ids, so sharing them across files would collapse unrelated
        groups. Keyed by budget id within the map — two budgets over the same
        categories are two different views and collapse independently.

        Unlike ADR-168's sidebar, a bare **set of collapsed keys** is right
        here: every group and section defaults to expanded, so there is no
        per-kind default for an explicit bool to protect.
        """
        if self._budget is None:
            return set()
        try:
            raw = self._repo.get_setting(self._collapse_setting_key())
            if not raw:
                return set()
            return set(json.loads(raw).get(str(self._budget.id), []))
        except Exception:  # noqa: BLE001
            # Corrupt or hand-edited setting must never break the screen —
            # everything simply shows expanded.
            return set()

    def _save_collapsed(self) -> None:
        """Best-effort persist — a failed write must never break the toggle."""
        if self._budget is None:
            return
        try:
            raw = self._repo.get_setting(self._collapse_setting_key())
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:  # noqa: BLE001
            data = {}
        if self._collapsed:
            data[str(self._budget.id)] = sorted(self._collapsed)
        else:
            data.pop(str(self._budget.id), None)
        try:
            self._repo.set_setting(
                self._collapse_setting_key(), json.dumps(data),
            )
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()

    def _toggle_collapse(self, key: str) -> None:
        if key in self._collapsed:
            self._collapsed.discard(key)
        else:
            self._collapsed.add(key)
        self._save_collapsed()
        model = self._table.model()
        if isinstance(model, BudgetMatrixModel):
            model.set_matrix(self._matrix, self._collapsed)
        self._monthly.set_collapsed(self._collapsed)

    def _on_cell_double_clicked(self, index) -> None:
        """Double-click an Actual cell → the transactions behind it (ADR-058).
        Budget cells edit; Actual/Diff are non-editable, so a double-click
        there is free to drill. Works on line Actuals, the Unbudgeted row, and
        a section's subtotal Actual."""
        if self._budget is None or self._matrix is None:
            return
        model = self._table.model()
        if model is None or not index.isValid() or index.column() < 1:
            return
        # The far-right Total column is a year sum — nothing to drill into.
        if index.column() > len(self._matrix.months):
            return
        row = model._rows[index.row()]
        # Net is a derived bottom line (no single bucket of txns) — skip it.
        if row.metric != "actual" or row.kind == _NET:
            return
        month = self._matrix.months[index.column() - 1]
        if row.kind == _SUBTOTAL:
            section = row.matrix_row  # bc.MatrixSection
            self._drill("section", None, section.kind, month,
                        f"{section.title} — all")
        else:
            mr = row.matrix_row  # bc.MatrixRow
            if mr.is_unbudgeted:
                self._drill("unbudgeted", None, mr.kind, month,
                            f"Unbudgeted {mr.kind}")
            elif mr.is_group:
                # A group's Actual is its whole subtree, so its drill must be
                # too — 'line' would show only the residual and contradict the
                # number just double-clicked (ADR-170).
                self._drill("group", mr.category_id, mr.kind, month,
                            f"{mr.label} — all")
            else:
                self._drill("line", mr.category_id, mr.kind, month, mr.label)

    def _drill(
        self, mode: str, target_cat, section_kind: str, month: str, label: str,
    ) -> None:
        """Recompute the exact bucketed perimeter txns for one cell and open a
        drill-down. Mirrors `compute_matrix`'s bucketing so the list reconciles
        with the Actual."""
        budget = self._budget
        ptxns = self._repo.list_perimeter_txns(
            budget.id, f"{month}-01", f"{month}-31",
        )
        parent_map = self._repo.category_parent_map()
        kind_map = self._repo.category_kind_map()
        budgeted_ids = {
            ln.category_id for ln in self._repo.list_budget_lines(budget.id)
        }
        ids: set[int] = set()
        net = Decimal("0.00")
        for t in ptxns:
            bucket = bc.nearest_budgeted_ancestor(
                t.category_id, parent_map, budgeted_ids,
            )
            if mode == "line":
                keep = bucket == target_cat
            elif mode == "group":
                # The group's roll-up: every txn whose bucket is the group's
                # category or a budgeted category beneath it — the exact set
                # `_rollup_cells` summed (ADR-170).
                keep = bucket is not None and bc.is_ancestor_or_self(
                    target_cat, bucket, parent_map,
                )
            elif mode == "unbudgeted":
                keep = bucket is None and kind_map.get(t.category_id) == section_kind
            else:  # section — everything in this kind, this month
                keep = kind_map.get(t.category_id) == section_kind
            if keep:
                ids.add(t.id)
                net += t.amount
        if not ids:
            QMessageBox.information(
                self, "No transactions",
                f"No transactions for {label} in {_fmt_month(month)}.",
            )
            return
        win = BudgetDrillDownWindow(
            self._repo, txn_ids=ids,
            title=f"{label} · {_fmt_month(month)}", net=net,
            display_ccy=self._display_ccy(), parent=self,
        )
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(
            lambda *_: self._drill_wins.remove(win)
            if win in self._drill_wins else None
        )
        self._drill_wins.append(win)
        win.show()

    # ── per-line context menu (R2: rollover / role / remove) ──

    def _on_context_menu(self, pos) -> None:
        """Right-click a budget line → toggle its rollover policy, change its
        role, or remove it from the budget. Only real budgeted lines have a
        menu — section headers, subtotals, and the synthetic Unbudgeted row
        carry no ``line_id``."""
        model = self._table.model()
        if not isinstance(model, BudgetMatrixModel):
            return
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        row = model._rows[index.row()]
        if row.kind != _METRIC or row.matrix_row.is_unbudgeted:
            return
        mr = row.matrix_row  # bc.MatrixRow
        line_id = mr.line_id
        menu = QMenu(self)

        if mr.is_group:
            # A group header is a roll-up over several lines, each with its own
            # rollover and role — there is no single policy to toggle here, and
            # silently applying one to the parent's own line would be a lie.
            # Removing the parent line is still meaningful (its children stay,
            # promoted to the top of the section), so that alone is offered.
            drop = QAction("Remove this group’s own line from budget", menu)
            drop.setToolTip(
                "Removes the group’s ‘Everything else’ line. Its budgeted "
                "children stay in the budget."
            )
            drop.triggered.connect(
                lambda _c=False, lid=line_id, lbl=mr.label:
                self._remove_line(lid, lbl)
            )
            menu.addAction(drop)
            menu.exec(self._table.viewport().mapToGlobal(pos))
            return

        roll = QAction("Rolls over unspent", menu)
        roll.setCheckable(True)
        roll.setChecked(mr.rollover == "accumulate")
        roll.toggled.connect(lambda on, lid=line_id: self._set_rollover(lid, on))
        menu.addAction(roll)

        # Role (bills / saving / discretionary) is an expense-side concept; it
        # has no meaning for income, so the submenu is hidden there (mirrors the
        # Setup dialog, which omits role for income lines).
        if mr.kind != "income":
            role_menu = menu.addMenu("Role")
            group = QActionGroup(role_menu)
            group.setExclusive(True)
            for r in BUDGET_ROLES:
                act = QAction(_ROLE_LABELS[r], role_menu)
                act.setCheckable(True)
                act.setChecked(mr.role == r)
                act.triggered.connect(
                    lambda _checked=False, lid=line_id, rr=r:
                    self._set_role(lid, rr)
                )
                group.addAction(act)
                role_menu.addAction(act)

        # ADR-094: bill — link this envelope to a scheduled transaction (or
        # unlink). Only on the spending side (income isn't a bill).
        if mr.kind != "income":
            menu.addSeparator()
            if mr.scheduled_txn_id is None:
                bill = QAction("Make this a bill…", menu)
                bill.triggered.connect(
                    lambda _c=False, lid=line_id, cid=mr.category_id:
                    self._make_bill(lid, cid)
                )
                menu.addAction(bill)
            else:
                unbill = QAction("Remove bill (keep schedule)", menu)
                unbill.triggered.connect(
                    lambda _c=False, lid=line_id: self._unlink_bill(lid)
                )
                menu.addAction(unbill)

        menu.addSeparator()
        remove = QAction("Remove from budget", menu)
        remove.triggered.connect(
            lambda _checked=False, lid=line_id, lbl=mr.label:
            self._remove_line(lid, lbl)
        )
        menu.addAction(remove)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _make_bill(self, line_id: int, category_id: Optional[int]) -> None:
        """Mark a budget line as a bill (ADR-094): collect the schedule in the
        full Schedule dialog (seeded from the line's category), create it, and
        link it to the line. The paying account is chosen there."""
        from mfl_desktop.ui.schedule_dialog import ScheduleDialog, ScheduleSeed
        accounts = self._repo.list_accounts()
        if not accounts:
            QMessageBox.information(
                None, "Make this a bill",
                "Create an account before scheduling a bill.",
            )
            return
        seed = ScheduleSeed(category_id=category_id, cadence="monthly")
        dlg = ScheduleDialog(
            accounts=accounts, categories=self._repo.list_categories_flat(),
            seed=seed, parent=None,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        if v is None:
            return
        try:
            sid = self._repo.create_scheduled_txn(
                account_id=v.account_id, payee_name=v.payee_name,
                category_id=v.category_id,
                transfer_to_account_id=v.transfer_to_account_id,
                estimated_amount=v.estimated_amount, variable=v.variable,
                memo=v.memo, cadence=v.cadence, anchor_date=v.anchor_date,
                next_due_date=v.next_due_date, end_date=v.end_date,
                auto_post=v.auto_post, notes=v.notes,
            )
            self._repo.set_budget_line_schedule(line_id, sid)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(None, "Make this a bill", str(e))
            return
        self._render()

    def _unlink_bill(self, line_id: int) -> None:
        """Demote a bill line back to a plain envelope; the schedule survives
        (manage it in Manage ▸ Schedules)."""
        try:
            self._repo.set_budget_line_schedule(line_id, None)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(None, "Remove bill", str(e))
            return
        self._render()

    def _set_rollover(self, line_id: int, on: bool) -> None:
        try:
            self._repo.update_budget_line(
                line_id, rollover="accumulate" if on else "none",
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not change rollover", str(e))
            return
        self._render()

    def _set_role(self, line_id: int, role: str) -> None:
        try:
            self._repo.update_budget_line(line_id, role=role)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not change role", str(e))
            return
        self._render()

    def _remove_line(self, line_id: int, label: str) -> None:
        # parent=None on the confirm — same macOS cascade-close avoidance as the
        # other budget-window dialogs (ADR-058).
        if QMessageBox.question(
            None, "Remove from budget",
            f"Remove ‘{label}’ from this budget? Its monthly amounts are "
            f"deleted; the category and its transactions are untouched.",
        ) != QMessageBox.Yes:
            return
        try:
            self._repo.delete_budget_line(line_id)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not remove line", str(e))
            return
        self._render()

    # ── budget verbs ──

    def _on_new(self) -> None:
        # parent=None on all budget-window dialogs — see _on_setup (ADR-058
        # macOS child-modal close-cascade).
        name, ok = QInputDialog.getText(None, "New budget", "Name:")
        if not ok or not name.strip():
            return
        year, ok = QInputDialog.getInt(
            None, "New budget", "Start year (budget runs Jan–Dec):",
            date.today().year, 2000, 2100,
        )
        if not ok:
            return
        b = self._repo.create_budget(
            name=name.strip(), start_month=f"{year:04d}-01", length_months=12,
        )
        self._reload_budget_list(select_id=b.id)

    def _on_duplicate(self) -> None:
        if self._budget is None:
            return
        name, ok = QInputDialog.getText(
            None, "Duplicate budget", "New name:",
            text=f"{self._budget.name} (scenario)",
        )
        if not ok or not name.strip():
            return
        b = self._repo.duplicate_budget(self._budget.id, name.strip())
        self._reload_budget_list(select_id=b.id)

    def _on_rename(self) -> None:
        if self._budget is None:
            return
        name, ok = QInputDialog.getText(
            None, "Rename budget", "Name:", text=self._budget.name,
        )
        if not ok or not name.strip():
            return
        self._repo.rename_budget(self._budget.id, name.strip())
        self._reload_budget_list(select_id=self._budget.id)

    def _on_delete(self) -> None:
        if self._budget is None:
            return
        if QMessageBox.question(
            None, "Delete budget",
            f"Delete budget ‘{self._budget.name}’? This removes its accounts, "
            f"categories, and all monthly amounts.",
        ) != QMessageBox.Yes:
            return
        self._repo.delete_budget(self._budget.id)
        self._reload_budget_list(select_id=None)

    def _on_period(self) -> None:
        if self._budget is None:
            return
        dlg = _PeriodDialog(self._budget, parent=None)
        if dlg.exec() != QDialog.Accepted:
            return
        self._repo.set_budget_period(
            self._budget.id, start_month=dlg.start_month(),
            length_months=dlg.length(),
        )
        self._budget = self._repo.get_budget(self._budget.id)
        self._render()

    def _on_setup(self) -> None:
        if self._budget is None:
            return
        # parent=None (not self): a modal dialog parented to this window makes
        # macOS cascade a spurious Close to the parent when the dialog (or its
        # nested chooser) closes — vanishing the budget window (ADR-058). A
        # standalone app-modal dialog has no parent to cascade to.
        dlg = BudgetSetupDialog(self._repo, self._budget, parent=None)
        if dlg.exec() == QDialog.Accepted:
            self._render()

    # ── refresh on activation ──

    def event(self, ev):  # noqa: N802 (Qt override)
        if (
            ev.type() == QEvent.WindowActivate
            and self._budget is not None
            and QApplication.activeModalWidget() is None
        ):
            # Defer the refresh to the next event-loop turn — never rebuild the
            # model *inside* the activate event (swapping/resetting a model
            # mid-event-handling, with an editor possibly open, is what tore the
            # window down on macOS). singleShot(0) runs it cleanly afterwards.
            QTimer.singleShot(0, self._refresh_on_activate)
        return super().event(ev)

    def _refresh_on_activate(self) -> None:
        # Re-fetch in case the budget was edited elsewhere. Belt-and-braces: an
        # exception must never escape (it's a queued callback now, not an event
        # override, but a crash here is still bad). Skip if the window is gone.
        try:
            if self._budget is not None and self._repo.is_open():
                self._render()
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()


def _ask_copy_forward_scope(parent, month_label: str) -> Optional[str]:
    """Ask how far to propagate a budget edit (ADR-058 D1). Returns
    'one' / 'forward' / 'all', or None if cancelled."""
    box = QMessageBox(parent)
    box.setWindowTitle("Apply budget amount")
    box.setText(f"Apply this amount to which months?")
    box.setInformativeText(
        f"You edited {month_label}. Copy it forward, or keep it to this month?"
    )
    just = box.addButton("Just this month", QMessageBox.AcceptRole)
    fwd = box.addButton("This + later months", QMessageBox.AcceptRole)
    all_btn = box.addButton("All months", QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(just)
    box.exec()
    clicked = box.clickedButton()
    if clicked is just:
        return "one"
    if clicked is fwd:
        return "forward"
    if clicked is all_btn:
        return "all"
    return None


class _PeriodDialog(QDialog):
    """Edit a budget's period — start month + length in months."""

    def __init__(self, budget: Budget, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Budget period")
        from PySide6.QtWidgets import QFormLayout

        form = QFormLayout(self)
        self._year = QSpinBox()
        self._year.setRange(2000, 2100)
        self._year.setValue(int(budget.start_month[:4]))
        form.addRow("Start year:", self._year)

        self._month = QComboBox()
        for i in range(1, 13):
            self._month.addItem(_MONTH_ABBR[i], i)
        self._month.setCurrentIndex(int(budget.start_month[5:7]) - 1)
        form.addRow("Start month:", self._month)

        self._length = QSpinBox()
        self._length.setRange(1, 36)
        self._length.setValue(budget.length_months)
        form.addRow("Length (months):", self._length)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def start_month(self) -> str:
        return f"{self._year.value():04d}-{self._month.currentData():02d}"

    def length(self) -> int:
        return self._length.value()
