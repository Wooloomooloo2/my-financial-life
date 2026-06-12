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

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
from mfl_desktop.db.repository import BUDGET_ROLES, Budget, Repository
from mfl_desktop.ui.budget_drilldown_window import BudgetDrillDownWindow
from mfl_desktop.ui.budget_monthly_view import BudgetMonthlyView
from mfl_desktop.ui.budget_setup_dialog import BudgetSetupDialog

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

# Row kinds in the flattened model.
_SECTION = "section"
_METRIC = "metric"        # one of budget / actual / diff for a line
_SUBTOTAL = "subtotal"
_NET = "net"              # bottom-line Net (Income − Expenses) metric row
_NET_HEADER = "net_header"

_NET_TITLE = "Net (Income − Expenses)"

_OVER = QColor("#b91c1c")     # red-700 — over budget / deficit
_UNDER = QColor("#15803d")    # green-700 — under budget / surplus
_SECTION_BG = QColor("#e2e8f0")   # slate-200
_SUBTOTAL_BG = QColor("#f1f5f9")  # slate-100
_TODAY_BG = QColor("#eff6ff")     # blue-50 — today's month column
_TOTAL_BG = QColor("#f8fafc")     # slate-50 — far-right Total column
_ROLLOVER_BG = QColor("#fef9c3")  # amber-100 — a Budget cell with carried-in rollover
_MUTED = QColor("#64748b")        # slate-500
_ZERO_D = Decimal("0.00")


def _fmt(value: Decimal) -> str:
    """'1,234.56' / '-1,234.56'. Currency symbol lives in the header, not in
    every cell, to keep the grid scannable."""
    return f"{value:,.2f}"


def _fmt_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{_MONTH_ABBR[m]} {y % 100:02d}"


# ── flattened row spec ─────────────────────────────────────────────────────


class _Row:
    __slots__ = ("kind", "section_idx", "matrix_row", "metric")

    def __init__(self, kind, section_idx, matrix_row=None, metric=None):
        self.kind = kind
        self.section_idx = section_idx
        self.matrix_row = matrix_row   # bc.MatrixRow or bc.MatrixSection
        self.metric = metric           # 'budget' | 'actual' | 'diff'


class BudgetMatrixModel(QAbstractTableModel):
    """Flattens a BudgetMatrix into a (Budget/Actual/Diff)-per-line table.

    Column 0 is the row label; columns 1..N are the budget's months. Only
    Budget metric cells on real (non-Unbudgeted) lines are editable; committing
    one routes through ``edit_cb(line_id, month, amount) -> bool``.
    """

    def __init__(self, matrix: bc.BudgetMatrix, edit_cb) -> None:
        super().__init__()
        self._m = matrix
        self._edit_cb = edit_cb
        self._rows: list[_Row] = []
        self._today_col: Optional[int] = None
        self._collapsed: set[int] = set()      # collapsed section indices
        self._net_budget: list[Decimal] = []
        self._net_actual: list[Decimal] = []
        self._rebuild_rows()

    def set_matrix(self, matrix: bc.BudgetMatrix) -> None:
        """Replace the model's data **in place** (one persistent model, reset
        around the swap). Reusing the model rather than ``setModel`` with a
        fresh one avoids orphaning an open inline editor — which Qt flags as
        'commitData called with an editor that does not belong to this view'
        and which, mid-event-handler on macOS, can tear the window down.
        Collapse state is preserved across the refresh."""
        self.beginResetModel()
        self._m = matrix
        self._rebuild_rows()
        self.endResetModel()

    def toggle_section(self, section_idx: int) -> None:
        """Expand/collapse a section (Income / Expenses / Transfers)."""
        self.beginResetModel()
        if section_idx in self._collapsed:
            self._collapsed.discard(section_idx)
        else:
            self._collapsed.add(section_idx)
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
            sign = Decimal(1) if s.kind == "income" else Decimal(-1)
            for mi in range(n):
                nb[mi] += sign * s.subtotal[mi].allocation
                na[mi] += sign * s.subtotal[mi].actual
        self._net_budget, self._net_actual = nb, na

    def _rebuild_rows(self) -> None:
        self._collapsed = {
            si for si in self._collapsed if si < len(self._m.sections)
        }
        self._compute_net()
        self._rows = []
        for si, section in enumerate(self._m.sections):
            self._rows.append(_Row(_SECTION, si, section))
            if si in self._collapsed:
                continue
            for mr in section.rows:
                for metric in ("budget", "actual", "diff"):
                    self._rows.append(_Row(_METRIC, si, mr, metric))
            # The subtotal is only meaningful when more than one row feeds it.
            # A section with a single row (e.g. only an Unbudgeted row, or one
            # budgeted line) would just duplicate that row and read as
            # double-counting — so skip it.
            if len(section.rows) >= 2:
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
                    return _SECTION_BG
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
                return _SECTION_BG
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
                if metric == "budget":
                    # A ↻ marks a line whose unspent budget rolls forward, so
                    # the policy is readable at a glance (toggle via right-click).
                    if not mr.is_unbudgeted and mr.rollover == "accumulate":
                        return f"{mr.label}  {_ROLLOVER_GLYPH}"
                    return mr.label
                return "  Actual" if metric == "actual" else "  Diff"
            if (
                role == Qt.ToolTipRole
                and metric == "budget"
                and not is_summary
                and not mr.is_unbudgeted
            ):
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
                return _MUTED
            if role == Qt.BackgroundRole and is_summary:
                return _SUBTOTAL_BG
            if role == Qt.FontRole and (is_summary or metric == "budget"):
                f = QFont()
                f.setBold(metric == "budget" and not mr.is_unbudgeted
                          if not is_summary else True)
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
                return _MUTED
            if role == Qt.BackgroundRole and is_total:
                return _TOTAL_BG
            return None

        # Rolled-over carry on a (month) Budget cell: annotate it so Budget /
        # Actual / Diff visibly reconcile (Diff uses available = alloc + carry).
        carry = (
            mr.cells[month_idx].carry_in
            if (row.kind == _METRIC and metric == "budget" and not is_total)
            else _ZERO_D
        )

        if role == Qt.DisplayRole:
            if metric == "diff" and value > 0:
                return f"+{_fmt(value)}"
            if carry != 0:
                return f"{_fmt(value)}  ({carry:+,.2f})"
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
                return _OVER
            if value > 0:
                return _UNDER
        if role == Qt.BackgroundRole:
            if carry != 0:
                return _ROLLOVER_BG
            if is_summary:
                return _SUBTOTAL_BG
            if is_total:
                return _TOTAL_BG
            if col == self._today_col:
                return _TODAY_BG
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
            and not row.matrix_row.is_unbudgeted
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

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 10)

        # Header: budget picker + verbs.
        header = QHBoxLayout()
        header.addWidget(QLabel("Budget:"))
        self._picker = QComboBox()
        self._picker.setMinimumWidth(200)
        self._picker.currentIndexChanged.connect(self._on_pick_budget)
        header.addWidget(self._picker)
        for label, slot in (
            ("New…", self._on_new),
            ("Duplicate…", self._on_duplicate),
            ("Rename…", self._on_rename),
            ("Delete", self._on_delete),
            ("Period…", self._on_period),
            ("Set up…", self._on_setup),
        ):
            b = QPushButton(label)
            b.clicked.connect(slot)
            header.addWidget(b)
        header.addStretch(1)
        # Annual matrix ↔ monthly progress view toggle (R3).
        header.addWidget(QLabel("View:"))
        self._view = QComboBox()
        self._view.addItem("Annual", 0)
        self._view.addItem("Monthly", 1)
        self._view.currentIndexChanged.connect(self._on_view_changed)
        header.addWidget(self._view)
        root.addLayout(header)

        # Soft Unallocated indicator + missing-rate banner.
        self._info_label = QLabel("")
        self._info_label.setTextFormat(Qt.RichText)
        root.addWidget(self._info_label)

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
        )

        # The annual matrix (page 0) and monthly view (page 1) share the
        # central area; the View toggle flips between them.
        self._stack = QStackedWidget()
        self._stack.addWidget(self._table)
        self._stack.addWidget(self._monthly)
        root.addWidget(self._stack, stretch=1)

        self._empty_label = QLabel(
            "No budget yet. Click New… to create one, then Set up… to choose "
            "accounts and categories."
        )
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet("color: #64748b;")
        root.addWidget(self._empty_label)

        self.setCentralWidget(central)
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
        self._render()

    def _on_view_changed(self) -> None:
        """Flip between the annual matrix and the monthly progress view."""
        self._stack.setCurrentIndex(self._view.currentData() or 0)

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
        matrix = bc.compute_matrix(
            budget=budget, lines=lines, allocations=allocations,
            perimeter_txns=ptxns, parent_map=self._repo.category_parent_map(),
            kind_map=self._repo.category_kind_map(), pool=pool,
            excluded_accounts=excluded, display_ccy=ccy,
            today_month=today_month,
        )

        self._matrix = matrix
        model = self._table.model()
        if isinstance(model, BudgetMatrixModel):
            # Update the existing model in place — no model swap under any open
            # editor (see BudgetMatrixModel.set_matrix).
            model.set_matrix(matrix)
        else:
            model = BudgetMatrixModel(matrix, self._on_edit_allocation)
            self._table.setModel(model)
        self._table.setColumnWidth(0, 240)
        for c in range(1, model.columnCount()):
            self._table.setColumnWidth(c, 86)
        # The Total column holds year-sized sums — give it a little more room.
        self._table.setColumnWidth(model.columnCount() - 1, 104)

        # Feed the same matrix to the monthly view (R3) so both pages stay in
        # lock-step off one computation — the single source of budget truth.
        self._monthly.set_data(budget, matrix)

        # Soft Unallocated indicator for the focused (today's) month, else first.
        focus_idx = months.index(today_month) if today_month in months else 0
        assigned = matrix.assigned_by_month[focus_idx]
        unalloc = pool - assigned
        focus_label = _fmt_month(months[focus_idx])
        colour = "#b91c1c" if unalloc < 0 else "#15803d"
        info = (
            f"Pool: <b>{ccy} {_fmt(pool)}</b> &nbsp;·&nbsp; "
            f"Assigned ({focus_label}): <b>{_fmt(assigned)}</b> &nbsp;·&nbsp; "
            f"Unallocated: <b style='color:{colour}'>{_fmt(unalloc)}</b>"
        )
        if excluded:
            # Exclusions now carry their own reason (no FX rate, or an
            # available-credit account with no limit set) — see
            # Repository.compute_perimeter_pool (ADR-058 R4a).
            info += (
                f" &nbsp;—&nbsp; <span style='color:#b45309'>"
                f"{len(excluded)} account(s) excluded from the pool: "
                f"{', '.join(excluded)}</span>"
            )
        self._info_label.setText(info)
        self.setWindowTitle(f"Budget — {budget.name}")

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
        """Single-click a section header → expand/collapse that section."""
        model = self._table.model()
        if not isinstance(model, BudgetMatrixModel) or not index.isValid():
            return
        row = model._rows[index.row()]
        if (
            row.kind == _SECTION
            and 0 <= row.section_idx < len(model._m.sections)
        ):
            model.toggle_section(row.section_idx)

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

        menu.addSeparator()
        remove = QAction("Remove from budget", menu)
        remove.triggered.connect(
            lambda _checked=False, lid=line_id, lbl=mr.label:
            self._remove_line(lid, lbl)
        )
        menu.addAction(remove)

        menu.exec(self._table.viewport().mapToGlobal(pos))

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
