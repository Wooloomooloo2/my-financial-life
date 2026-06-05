"""The Budget screen — non-modal main window opened from Budget ▸ Open….

Layout (top-down):

- Header strip: budget name + period (prev/next) + Setup… button
- Cash-on-hand reality-check badge (sum of in-perimeter account balances)
- Four-tile Simplifi summary strip: Income after bills & saving / Planned
  spending / Other spending / Available
- Per-category cards grouped by role: Income / Bills & saving /
  Discretionary; each card has a coloured progress bar plus left /
  spent / txn-count line and a cadence subtitle when the budget is set
  on a non-monthly cadence.

Refreshes on `WindowActivate` so flipping back from the register after
an edit reflects the new actuals. The owning RegisterWindow can also
call ``reload()`` directly after a known-relevant mutation.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.budget_calc import (
    BudgetCardData,
    BudgetSummary,
    cadence_period_containing,
    calendar_month_period,
    compute_budget_view,
    compute_burn_down,
    compute_summary_breakdown,
    nearest_budgeted_ancestor,
)
from mfl_desktop.db.repository import Budget, Repository
from mfl_desktop.ui.budget_setup_dialog import BudgetSetupDialog
from mfl_desktop.ui.burn_down_chart import BurnDownChart
from mfl_desktop.ui.proportional_bar import BarSegment, ProportionalBar


_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

_ROLE_HEADERS = [
    ("income",        "Income"),
    ("bills",         "Bills"),
    ("saving",        "Saving"),
    ("discretionary", "Discretionary"),
]

_CADENCE_LABELS = {
    "weekly":    "weekly",
    "biweekly":  "bi-weekly",
    "monthly":   "monthly",
    "quarterly": "quarterly",
    "annual":    "annually",
}


class BudgetWindow(QMainWindow):
    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._budget: Budget = repo.get_or_create_default_budget()

        today = date.today()
        self._period_year = today.year
        self._period_month = today.month

        self.resize(1080, 760)
        self.setWindowTitle("Budget")
        self.setStatusBar(QStatusBar(self))

        # ── header strip ──
        self._budget_name_label = QLabel()
        self._budget_name_label.setStyleSheet("font-size: 16pt; font-weight: 600;")
        self._period_label = QLabel()
        self._period_label.setStyleSheet("font-size: 14pt; color: #444;")

        prev_btn = QPushButton("◀")
        next_btn = QPushButton("▶")
        prev_btn.setFixedWidth(36)
        next_btn.setFixedWidth(36)
        prev_btn.clicked.connect(lambda: self._shift_period(-1))
        next_btn.clicked.connect(lambda: self._shift_period(+1))

        setup_btn = QPushButton("Setup…")
        setup_btn.clicked.connect(self._on_setup)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(16, 12, 16, 4)
        header_row.addWidget(self._budget_name_label)
        header_row.addSpacing(20)
        header_row.addWidget(prev_btn)
        header_row.addWidget(self._period_label)
        header_row.addWidget(next_btn)
        header_row.addStretch(1)
        header_row.addWidget(setup_btn)

        # ── cash badge ──
        self._cash_label = QLabel()
        self._cash_label.setStyleSheet("color: #555; padding-left: 16px;")

        # ── summary tiles ──
        self._tile_income = _SummaryTile("Income after bills & saving", positive_is_good=True)
        self._tile_planned = _SummaryTile("Planned spending", positive_is_good=False)
        self._tile_other = _SummaryTile("Other spending", positive_is_good=False)
        self._tile_available = _SummaryTile("Available", positive_is_good=True)

        tile_row = QHBoxLayout()
        tile_row.setContentsMargins(12, 4, 12, 4)
        tile_row.setSpacing(8)
        tile_row.addWidget(self._tile_income)
        tile_row.addWidget(self._tile_planned)
        tile_row.addWidget(self._tile_other)
        tile_row.addWidget(self._tile_available)

        # ── proportional summary bar ──
        self._bar_label = QLabel("Spending plan")
        self._bar_label.setStyleSheet("color: #555; padding-left: 16px;")
        self._summary_bar = ProportionalBar()
        bar_holder = QWidget()
        bar_layout = QVBoxLayout(bar_holder)
        bar_layout.setContentsMargins(12, 4, 12, 4)
        bar_layout.setSpacing(2)
        bar_layout.addWidget(self._bar_label)
        bar_layout.addWidget(self._summary_bar)

        # ── burn-down chart ──
        self._burn_down = BurnDownChart()
        burn_holder = QWidget()
        burn_layout = QVBoxLayout(burn_holder)
        burn_layout.setContentsMargins(12, 4, 12, 4)
        burn_layout.addWidget(self._burn_down)

        # ── cards area ──
        self._cards_holder = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_holder)
        self._cards_layout.setContentsMargins(16, 8, 16, 16)
        self._cards_layout.setSpacing(12)
        self._cards_layout.addStretch(1)  # pushes content up when sparse

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._cards_holder)
        scroll.setFrameShape(QFrame.NoFrame)

        # ── compose ──
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header_row)
        layout.addWidget(self._cash_label)
        layout.addLayout(tile_row)
        layout.addWidget(bar_holder)
        layout.addWidget(burn_holder)
        layout.addWidget(scroll, stretch=1)
        self.setCentralWidget(central)

        self.reload()

    # ── period navigation ──

    def _shift_period(self, direction: int) -> None:
        m = self._period_month + direction
        y = self._period_year
        if m < 1:
            m = 12
            y -= 1
        elif m > 12:
            m = 1
            y += 1
        self._period_year = y
        self._period_month = m
        self.reload()

    # ── refresh ──

    def event(self, ev):
        # Repaint with fresh data when the user clicks back from the
        # register; cheap enough at MFL's scale that re-querying on every
        # activation is fine.
        if ev.type() == QEvent.WindowActivate:
            self.reload()
        return super().event(ev)

    def reload(self) -> None:
        self._budget = self._repo.get_or_create_default_budget()
        period_start, period_end = calendar_month_period(
            self._period_year, self._period_month,
        )
        budget_cats = self._repo.list_budget_categories(self._budget.id)
        perimeter_txns = self._repo.list_perimeter_txns(
            self._budget.id, period_start, period_end,
        )
        cash = self._repo.compute_perimeter_cash_on_hand(self._budget.id)
        parent_map = self._repo.category_parent_map()

        # Round-C additions: full-cadence-period actuals (so each non-monthly
        # card can show "this year: £X of £Y" in its subtitle), and the sum
        # of un-posted scheduled outflows due in the screen period, bucketed
        # per card. Both are optional inputs to compute_budget_view; passing
        # empty dicts is equivalent to round-B behaviour.
        today_iso = date.today().isoformat()
        cadence_actuals, cadence_labels = self._compute_cadence_period_actuals(
            budget_cats, parent_map, today_iso,
        )
        scheduled_due = self._compute_scheduled_due_per_card(
            budget_cats, parent_map, period_start, period_end,
        )

        summary, cards = compute_budget_view(
            budget_categories=budget_cats,
            perimeter_txns=perimeter_txns,
            parent_map=parent_map,
            cash_on_hand=cash,
            period_start=period_start,
            period_end=period_end,
            cadence_period_actuals_by_category=cadence_actuals,
            cadence_period_label_by_category=cadence_labels,
            scheduled_due_by_category=scheduled_due,
        )

        # Header
        self._budget_name_label.setText(self._budget.name)
        self._period_label.setText(
            f"{_MONTH_NAMES[self._period_month - 1]} {self._period_year}"
        )
        perimeter_count = len(self._repo.list_budget_account_ids(self._budget.id))
        if perimeter_count == 0:
            self._cash_label.setText(
                "No accounts in this budget yet — click Setup… to choose."
            )
        else:
            self._cash_label.setText(
                f"Cash on hand: {summary.cash_on_hand:,.2f}  "
                f"across {perimeter_count} "
                f"account{'s' if perimeter_count != 1 else ''}"
            )

        # Tiles
        self._tile_income.set_value(summary.income_after_bills_and_saving)
        self._tile_planned.set_value(summary.planned_spending)
        self._tile_other.set_value(summary.other_spending)
        self._tile_available.set_value(summary.available)

        # Proportional summary bar
        breakdown = compute_summary_breakdown(summary)
        self._summary_bar.set_segments([
            BarSegment("Bills",            breakdown.bills,            QColor("#c2410c")),
            BarSegment("Saving",           breakdown.saving,           QColor("#7c3aed")),
            BarSegment("Planned spending", breakdown.planned_spending, QColor("#2563eb")),
            BarSegment("Other spending",   breakdown.other_spending,   QColor("#6b7280")),
            BarSegment("Available",        breakdown.available,        QColor("#16a34a")),
        ])
        self._update_bar_label(breakdown)

        # Burn-down chart
        burn = compute_burn_down(
            perimeter_txns=perimeter_txns,
            summary=summary,
            period_start=period_start,
            period_end=period_end,
        )
        self._burn_down.set_data(burn)

        # Cards
        self._rebuild_cards(cards, len(budget_cats) > 0)

    def _update_bar_label(self, breakdown) -> None:
        """Short legend line under the proportional bar — colour-keyed
        chips so the user can read the segments without hovering."""
        chip = lambda name, value, colour: (
            f"<span style='color:{colour};'>■</span> "
            f"{name} {value:,.2f}"
        )
        parts = [
            chip("Bills",   breakdown.bills,            "#c2410c"),
            chip("Saving",  breakdown.saving,           "#7c3aed"),
            chip("Planned", breakdown.planned_spending, "#2563eb"),
            chip("Other",   breakdown.other_spending,   "#6b7280"),
            chip("Avail.",  breakdown.available,        "#16a34a"),
        ]
        self._bar_label.setText(
            "Spending plan: " + "  ·  ".join(parts)
        )

    # ── round-C helpers ──

    def _compute_cadence_period_actuals(
        self,
        budget_cats,
        parent_map,
        today_iso: str,
    ) -> tuple[dict[int, Decimal], dict[int, str]]:
        """For each unique non-monthly cadence among the budgeted categories,
        run one perimeter-txn query over the cadence's calendar period
        containing today, then bucket-by-nearest-budgeted-ancestor. Each
        cadence's results contribute only to its own cards' subtitle —
        a "Holidays £1,800/year" card shows the year-to-date actuals; a
        "Pocket money £30/week" card shows this week's actuals.

        Monthly cadence is skipped because the screen's period IS the
        calendar month, so the per-card actuals are already correct."""
        budgeted_ids = {bc.category_id for bc in budget_cats}
        actuals: dict[int, Decimal] = {}
        labels: dict[int, str] = {}

        by_cadence: dict[str, list[int]] = {}
        for bc in budget_cats:
            if bc.cadence == "monthly":
                continue
            by_cadence.setdefault(bc.cadence, []).append(bc.category_id)

        for cadence, cat_ids in by_cadence.items():
            start, end, label = cadence_period_containing(cadence, today_iso)
            cadence_txns = self._repo.list_perimeter_txns(
                self._budget.id, start, end,
            )
            bucket_totals: dict[int, Decimal] = {}
            for txn in cadence_txns:
                if txn.amount >= 0:
                    # Income / refund flows aren't surfaced in the
                    # per-card spending subtitle — same magnitude
                    # convention as the screen-period card.
                    continue
                bucket = nearest_budgeted_ancestor(
                    txn.category_id, parent_map, budgeted_ids,
                )
                if bucket is None:
                    continue
                bucket_totals[bucket] = (
                    bucket_totals.get(bucket, Decimal("0")) + (-txn.amount)
                )
            for cid in cat_ids:
                actuals[cid] = bucket_totals.get(cid, Decimal("0"))
                labels[cid] = label

        return actuals, labels

    def _compute_scheduled_due_per_card(
        self,
        budget_cats,
        parent_map,
        period_start: str,
        period_end: str,
    ) -> dict[int, Decimal]:
        """Sum of estimated outflow magnitudes from un-posted schedules
        due inside the screen period, bucketed against the nearest
        budgeted ancestor. Overdue schedules from prior periods are
        excluded — they belong to a different month's budget."""
        budgeted_ids = {bc.category_id for bc in budget_cats}
        # Query all active schedules due through the period end, then
        # filter in Python to those with next_due_date >= period_start.
        candidates = self._repo.list_perimeter_schedules_due_through(
            self._budget.id, period_end,
        )
        out: dict[int, Decimal] = {}
        for sched in candidates:
            if sched.next_due_date < period_start:
                continue
            if sched.estimated_amount >= 0:
                # Income schedules don't contribute to the "expected
                # outflow" badge — keeping the card subtitle focused
                # on the spending question.
                continue
            bucket = nearest_budgeted_ancestor(
                sched.category_id, parent_map, budgeted_ids,
            )
            if bucket is None:
                continue
            out[bucket] = (
                out.get(bucket, Decimal("0")) + abs(sched.estimated_amount)
            )
        return out

    def _rebuild_cards(
        self,
        cards: list[BudgetCardData],
        has_any_budget: bool,
    ) -> None:
        # Clear existing children (everything except the trailing stretch).
        while self._cards_layout.count() > 1:
            item = self._cards_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not has_any_budget:
            empty = QLabel(
                "No budget categories yet.\n\nClick Setup… to add categories "
                "and set monthly / weekly / annual targets."
            )
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("color: #888; padding: 40px;")
            self._cards_layout.insertWidget(self._cards_layout.count() - 1, empty)
            return

        # Group cards: income kind first, then by role.
        groups: dict[str, list[BudgetCardData]] = {
            key: [] for key, _ in _ROLE_HEADERS
        }
        for c in cards:
            if c.kind == "income":
                groups["income"].append(c)
            elif c.role == "bills":
                groups["bills"].append(c)
            elif c.role == "saving":
                groups["saving"].append(c)
            else:
                groups["discretionary"].append(c)

        insert_at = self._cards_layout.count() - 1
        for key, header_text in _ROLE_HEADERS:
            group_cards = groups[key]
            if not group_cards:
                continue
            header = QLabel(header_text)
            header.setStyleSheet(
                "font-size: 12pt; font-weight: 600; color: #333; "
                "padding-top: 8px; padding-bottom: 4px;"
            )
            self._cards_layout.insertWidget(insert_at, header)
            insert_at += 1
            group_cards.sort(key=lambda c: c.label.lower())
            for c in group_cards:
                self._cards_layout.insertWidget(insert_at, _CategoryCard(c))
                insert_at += 1

    # ── setup ──

    def _on_setup(self) -> None:
        dialog = BudgetSetupDialog(self._repo, self._budget, parent=self)
        if dialog.exec() == BudgetSetupDialog.Accepted:
            self.reload()
            self.statusBar().showMessage("Budget updated.", 4000)


class _SummaryTile(QFrame):
    """One of the four top-strip tiles. Value colour flips depending on
    sign + whether positive is good for this tile (Available wants to be
    big and green; Planned/Other spending wants to be small)."""

    def __init__(self, title: str, *, positive_is_good: bool) -> None:
        super().__init__()
        self._positive_is_good = positive_is_good
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setStyleSheet(
            "QFrame { background: #f6f7f9; border: 1px solid #e1e3e6;"
            "         border-radius: 8px; }"
        )

        self._title_label = QLabel(title)
        self._title_label.setStyleSheet("color: #555; font-size: 10pt;")
        self._value_label = QLabel("—")
        value_font = QFont()
        value_font.setPointSize(18)
        value_font.setBold(True)
        self._value_label.setFont(value_font)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(self._title_label)
        layout.addWidget(self._value_label)

    def set_value(self, amount: Decimal) -> None:
        self._value_label.setText(f"{amount:,.2f}")
        # Colour decision: amounts at zero stay neutral; positive = green
        # if good, red if bad; negative is the inverse.
        if amount == 0:
            colour = "#333"
        elif (amount > 0) == self._positive_is_good:
            colour = "#1b8a3a"  # green
        else:
            colour = "#b3261e"  # red
        self._value_label.setStyleSheet(
            f"color: {colour};"
        )


class _CategoryCard(QFrame):
    """One row card for a single budgeted category. Progress bar tints
    red when over budget, green when under."""

    def __init__(self, data: BudgetCardData) -> None:
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background: white; border: 1px solid #e1e3e6;"
            "         border-radius: 6px; padding: 4px; }"
        )

        # Headline row: name + status text on the right
        name_label = QLabel(data.label)
        name_label.setStyleSheet("font-size: 11pt; font-weight: 600;")

        # Right-side status: "£X left" or "£X over"
        if data.period_left >= 0:
            status_text = f"{data.period_left:,.2f} left"
            status_colour = "#1b8a3a"
        else:
            status_text = f"{-data.period_left:,.2f} over"
            status_colour = "#b3261e"
        status_label = QLabel(status_text)
        status_label.setStyleSheet(
            f"font-size: 11pt; font-weight: 600; color: {status_colour};"
        )

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.addWidget(name_label)
        top_row.addStretch(1)
        top_row.addWidget(status_label)

        # Progress bar
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        bar_pct = _progress_pct(data.period_actual, data.period_budget)
        bar.setRange(0, 100)
        bar.setValue(bar_pct)
        over = data.period_left < 0
        bar.setStyleSheet(
            "QProgressBar { background: #eef0f3; border: 0; border-radius: 4px; } "
            "QProgressBar::chunk { background: "
            + ("#d23a2c" if over else "#3a9c54")
            + "; border-radius: 4px; }"
        )

        # Subtitle (round-C): "£X spent of £Y · N txns" plus a second line
        # for non-monthly cadences showing full-cadence-period progress,
        # and an "+£X expected" badge when schedules are due in the
        # screen period and haven't posted yet.
        n = data.period_txn_count
        expected_badge = (
            f"  ·  +{data.scheduled_due_in_period:,.2f} expected"
            if data.scheduled_due_in_period > 0 else ""
        )
        line1 = QLabel(
            f"{data.period_actual:,.2f} spent of "
            f"{data.period_budget:,.2f}  ·  "
            f"{n} txn{'s' if n != 1 else ''}"
            f"{expected_badge}"
        )
        line1.setStyleSheet("color: #666; font-size: 9pt;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        layout.addLayout(top_row)
        layout.addWidget(bar)
        layout.addWidget(line1)

        if data.cadence != "monthly":
            # Non-monthly cadence: show the user-entered figure at its
            # native cadence + the full-period progress so the user has
            # the long view without losing the monthly comparison.
            label_text = data.cadence_period_label or "this period"
            line2 = QLabel(
                f"{data.cadence_amount:,.2f} {_CADENCE_LABELS[data.cadence]}  ·  "
                f"{label_text}: "
                f"{data.cadence_period_actual:,.2f} of "
                f"{data.cadence_amount:,.2f}"
            )
            line2.setStyleSheet("color: #888; font-size: 8pt; font-style: italic;")
            layout.addWidget(line2)


def _progress_pct(actual: Decimal, budget: Decimal) -> int:
    """Percentage for the progress bar. Capped at 100 so the chunk doesn't
    overflow — the over-budget signal is the red tint + the status label,
    not a bar that fills past its track."""
    if budget <= 0:
        return 100 if actual > 0 else 0
    pct = int((actual / budget * 100).quantize(Decimal("1")))
    return max(0, min(100, pct))
