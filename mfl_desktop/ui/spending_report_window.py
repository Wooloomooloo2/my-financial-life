"""Spending Over Time — the first report.

Non-modal QMainWindow with a controls panel on the left and a stacked-bar
chart on the right. Each bar is one time bucket (week / month / quarter /
year); each stack segment within a bar is one top-level expense category
group (per ADR-018 grouping rule). An average line is drawn across the
chart; a summary strip at the bottom shows period total and average.

Pies are deliberately absent (the owner's standing rule). Income and
transfer transactions are excluded by definition — this is a *spending*
report, where spending is `-amount` on expense-kind categories so refunds
reduce the net (ADR-014 sign convention).

Renderer is the hand-rolled :class:`SpendingChart` paintEvent widget, per
ADR-026 (which winnowed the QtCharts / pyqtgraph / custom comparison down
to the custom variant).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import Repository
from mfl_desktop.reports import category_group_map
from mfl_desktop.ui.spending_chart import SpendingChart

# id of the seeded Uncategorised root — its toggle is separate from the
# category checklist per the user's spec.
UNCATEGORISED_ID = 1

_GRANULARITIES = [
    ("Weekly",    "week"),
    ("Monthly",   "month"),
    ("Quarterly", "quarter"),
    ("Annually",  "year"),
]
_GRANULARITY_LABEL_FOR_AVG = {
    "week": "week", "month": "month", "quarter": "quarter", "year": "year",
}


class SpendingReportWindow(QMainWindow):
    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Spending Over Time")
        self.resize(1240, 740)
        self._repo = repo

        # ── reference data for the controls (loaded once) ──
        self._all_accounts = repo.list_accounts()
        self._all_categories = repo.list_category_tree()
        self._categories_by_id = {c.id: c for c in self._all_categories}
        self._category_groups = category_group_map(self._all_categories)

        controls = self._build_controls()
        self._chart = SpendingChart()
        self._summary_label = QLabel()
        self._summary_label.setStyleSheet(
            "padding: 8px 12px; font-weight: bold;"
        )

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(controls)
        splitter.addWidget(self._chart)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 900])

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(splitter, stretch=1)
        central_layout.addWidget(self._summary_label)
        self.setCentralWidget(central)

        # Wire control changes to a debounce-free refresh — at ~1.3k rows
        # the round-trip is well under a frame.
        self._granularity_combo.currentIndexChanged.connect(self._refresh)
        self._date_from.dateChanged.connect(self._refresh)
        self._date_to.dateChanged.connect(self._refresh)
        self._accounts_list.itemChanged.connect(self._refresh)
        self._categories_list.itemChanged.connect(self._refresh)
        self._include_uncat_check.toggled.connect(self._refresh)

        self._refresh()

    # ── controls panel ──

    def _build_controls(self) -> QWidget:
        self._granularity_combo = QComboBox()
        for label, value in _GRANULARITIES:
            self._granularity_combo.addItem(label, userData=value)
        self._granularity_combo.setCurrentIndex(1)  # Monthly default

        today = QDate.currentDate()
        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDisplayFormat("yyyy-MM-dd")
        self._date_from.setDate(today.addYears(-1))
        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDisplayFormat("yyyy-MM-dd")
        self._date_to.setDate(today)

        # Accounts checklist — start with everything checked.
        self._accounts_list = QListWidget()
        self._accounts_list.setMaximumHeight(140)
        for acct in self._all_accounts:
            item = QListWidgetItem(acct.name)
            item.setData(Qt.UserRole, acct.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self._accounts_list.addItem(item)

        # Categories checklist — top-level expense groups only.
        # Uncategorised has its own toggle below, so it's excluded here.
        expense_group_ids: set[int] = set()
        for c in self._all_categories:
            if c.kind == "expense":
                expense_group_ids.add(self._category_groups[c.id])
        expense_group_ids.discard(UNCATEGORISED_ID)
        expense_groups = sorted(
            (self._categories_by_id[gid] for gid in expense_group_ids
             if gid in self._categories_by_id),
            key=lambda c: c.name.lower(),
        )
        self._categories_list = QListWidget()
        self._categories_list.setMaximumHeight(220)
        for g in expense_groups:
            item = QListWidgetItem(g.name)
            item.setData(Qt.UserRole, g.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self._categories_list.addItem(item)

        self._include_uncat_check = QCheckBox("Include Uncategorised")
        self._include_uncat_check.setChecked(True)

        controls = QWidget()
        form = QFormLayout(controls)
        form.addRow("Granularity:", self._granularity_combo)
        form.addRow("From:", self._date_from)
        form.addRow("To:", self._date_to)
        form.addRow(QLabel("Accounts:"))
        form.addRow(self._accounts_list)
        form.addRow(QLabel("Categories:"))
        form.addRow(self._categories_list)
        form.addRow(self._include_uncat_check)
        return controls

    # ── refresh / render ──

    def _refresh(self) -> None:
        granularity = self._granularity_combo.currentData() or "month"
        date_from = self._date_from.date().toString(Qt.ISODate)
        date_to = self._date_to.date().toString(Qt.ISODate)
        account_ids = self._checked_ids(self._accounts_list)
        if not account_ids:
            self._show_empty("Select at least one account.")
            return

        include_uncat = self._include_uncat_check.isChecked()
        rows = self._repo.spending_aggregates(
            date_from=date_from,
            date_to=date_to,
            granularity=granularity,
            account_ids=account_ids,
            include_uncategorised=include_uncat,
        )

        # Roll category_id → group_id, filter out groups the user has
        # unchecked in the categories list. Uncategorised is already
        # handled by the SQL include_uncategorised flag, so it always
        # passes the Python filter (it's a group too, just not in the
        # checklist).
        checked_group_ids = set(self._checked_ids(self._categories_list))
        spending: dict[tuple[int, str], int] = {}
        for r in rows:
            cid = r["category_id"]
            gid = self._category_groups.get(cid, cid)
            if gid != UNCATEGORISED_ID and gid not in checked_group_ids:
                continue
            key = (gid, r["bucket"])
            spending[key] = spending.get(key, 0) + r["spending_pence"]

        self._render(spending, granularity)

    def _render(
        self, spending: dict[tuple[int, str], int], granularity: str,
    ) -> None:
        buckets = sorted({key[1] for key in spending.keys()})
        if not buckets:
            self._show_empty("No spending in the selected range / filters.")
            return

        # Stable stack order: largest-total groups first so colour
        # assignment is consistent across refreshes.
        group_totals: dict[int, int] = {}
        for (gid, _), val in spending.items():
            group_totals[gid] = group_totals.get(gid, 0) + val
        groups_sorted_ids = sorted(group_totals.keys(),
                                   key=lambda g: -group_totals[g])
        groups: list[tuple[int, str]] = [
            (
                gid,
                self._categories_by_id[gid].name
                if gid in self._categories_by_id else f"id={gid}",
            )
            for gid in groups_sorted_ids
        ]

        total_pence = sum(spending.values())
        avg_pence = total_pence / len(buckets)
        avg_pounds = avg_pence / 100.0

        self._chart.render(
            buckets=buckets,
            groups=groups,
            spending=spending,
            avg_pounds=avg_pounds,
        )

        total_pounds = total_pence / 100.0
        gran_word = _GRANULARITY_LABEL_FOR_AVG[granularity]
        bucket_word = gran_word if len(buckets) == 1 else f"{gran_word}s"
        self._summary_label.setText(
            f"Total: £{total_pounds:,.2f}     "
            f"Average: £{avg_pounds:,.2f} / {gran_word}     "
            f"({len(buckets)} {bucket_word})"
        )

    def _show_empty(self, message: str) -> None:
        self._chart.show_empty(message)
        self._summary_label.setText(message)

    @staticmethod
    def _checked_ids(list_widget: QListWidget) -> list[int]:
        out: list[int] = []
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if item.checkState() == Qt.Checked:
                data = item.data(Qt.UserRole)
                if isinstance(data, int):
                    out.append(data)
        return out
