"""Net Worth — assets vs debts at a glance.

Three columns: Summary on the left (total + horizontal proportional bar +
colour-coded legend), Assets in the middle (grouped by family with each
account listed), Debts on the right (mirror of Assets in red). + Asset
and + Debt buttons at the bottom of their columns open the existing
AccountDialog so adding shows up in the report on accept.

Pies are deliberately substituted with a proportional bar — owner rule.
Investment and property balances use the same opening + sum(txn.amount)
formula as cash and credit; the schema's `valuation` table is reserved
for mark-to-market but isn't wired in v1 (ADR-019).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.account_types import ACCOUNT_TYPES
from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.ui.account_dialog import AccountDialog
from mfl_desktop.ui.proportional_bar import BarSegment, ProportionalBar


# Family → (display label, color, kind) where kind ∈ {"asset","debt"}.
# Order in this list is the display order in the summary + columns.
_FAMILY_VIEW: list[tuple[str, str, QColor, str]] = [
    ("investment", "Investments",   QColor("#2563eb"), "asset"),
    ("property",   "Property",      QColor("#14b8a6"), "asset"),
    ("vehicle",    "Vehicles",      QColor("#f59e0b"), "asset"),
    ("cash",       "Cash & Bank",   QColor("#22c55e"), "asset"),
    ("credit",     "Credit Cards",  QColor("#ec4899"), "debt"),
]

_ASSET_COLOR = QColor("#16a34a")   # column header
_DEBT_COLOR = QColor("#dc2626")


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
    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Net Worth")
        self.resize(1240, 720)
        self._repo = repo

        # ── columns ──
        self._summary_panel, self._summary_total_lbl, \
            self._bar, self._legend_layout = self._build_summary_panel()
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
        splitter.setSizes([400, 400, 400])

        self.setCentralWidget(splitter)
        self._refresh()

    # ── builders ──

    def _build_summary_panel(self) -> tuple[QWidget, QLabel, ProportionalBar, QVBoxLayout]:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        title = QLabel("Net Worth")
        title.setStyleSheet("color: #6b7280;")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 2)
        title.setFont(title_font)

        total_lbl = QLabel("£0.00")
        big = total_lbl.font()
        big.setPointSize(big.pointSize() + 16)
        big.setBold(True)
        total_lbl.setFont(big)

        bar = ProportionalBar()

        layout.addWidget(title)
        layout.addWidget(total_lbl)
        layout.addSpacing(6)
        layout.addWidget(bar)
        layout.addSpacing(8)

        # Legend rows are added dynamically by _refresh.
        legend = QVBoxLayout()
        legend.setContentsMargins(0, 0, 0, 0)
        legend.setSpacing(4)
        layout.addLayout(legend)
        layout.addStretch(1)
        return panel, total_lbl, bar, legend

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
        subhead.setStyleSheet("color: #6b7280; letter-spacing: 1px;")

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

    # ── data + render ──

    def _refresh(self) -> None:
        accounts = self._repo.list_accounts()
        balances = self._repo.compute_account_balances()

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
            if kind == "debt":
                # Liability balances are stored negative; display the
                # debt as a positive amount (£429 owed, not -£429).
                total = sum(
                    (-balances.get(m.id, Decimal("0.00")) for m in members),
                    start=Decimal("0.00"),
                )
            else:
                total = sum(
                    (balances.get(m.id, Decimal("0.00")) for m in members),
                    start=Decimal("0.00"),
                )
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

        # Proportional bar — assets only.
        self._bar.set_segments([
            BarSegment(label=ft.label, amount=ft.total, color=ft.color)
            for ft in family_totals if ft.kind == "asset"
        ])

        # Legend.
        self._rebuild_legend(family_totals)

        # Type-level totals power the Assets / Debts columns. The summary
        # panel stays family-level (above) so the proportional bar doesn't
        # explode into 5–10 segments.
        type_totals = self._compute_type_totals(by_family, balances)

        # Assets column header total + tree.
        self._assets_total_lbl.setText(self._format(asset_total))
        self._fill_tree(self._assets_tree, type_totals, kind="asset",
                        balances=balances)

        # Debts column header total + tree.
        self._debts_total_lbl.setText(self._format(debt_total))
        self._fill_tree(self._debts_tree, type_totals, kind="debt",
                        balances=balances)

    def _compute_type_totals(
        self,
        accounts_by_family: dict[str, list[AccountSummary]],
        balances: dict[int, Decimal],
    ) -> list[_TypeTotal]:
        """Roll up accounts by account.type — finer-grained than family —
        and pair each type with its family colour and kind."""
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
            if kind == "debt":
                total = sum(
                    (-balances.get(m.id, Decimal("0.00")) for m in members),
                    start=Decimal("0.00"),
                )
            else:
                total = sum(
                    (balances.get(m.id, Decimal("0.00")) for m in members),
                    start=Decimal("0.00"),
                )
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

    def _rebuild_legend(self, family_totals: list[_FamilyTotal]) -> None:
        # Drop every previous legend row.
        while self._legend_layout.count():
            item = self._legend_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        # Assets first.
        any_assets = any(ft.kind == "asset" and ft.total > 0 for ft in family_totals)
        any_debts = any(ft.kind == "debt" and ft.total > 0 for ft in family_totals)
        if any_assets:
            self._legend_layout.addWidget(self._heading_row("ASSETS"))
            for ft in family_totals:
                if ft.kind == "asset" and ft.total > 0:
                    self._legend_layout.addWidget(self._legend_row(ft))
        if any_debts:
            self._legend_layout.addWidget(self._heading_row("DEBTS"))
            for ft in family_totals:
                if ft.kind == "debt" and ft.total > 0:
                    self._legend_layout.addWidget(self._legend_row(ft))

    def _heading_row(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color: #6b7280; letter-spacing: 1px; margin-top: 8px;"
        )
        return lbl

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
        label.setStyleSheet("color: #111827;")

        amount = QLabel(self._format(ft.total))
        amount.setStyleSheet("color: #111827; font-weight: bold;")
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
        balances: dict[int, Decimal],
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
                bal = balances.get(acct.id, Decimal("0.00"))
                shown = -bal if kind == "debt" else bal
                child = QTreeWidgetItem([
                    acct.name,
                    self._format(shown),
                ])
                child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                group_item.addChild(child)
            group_item.setExpanded(True)

    # ── actions ──

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
            )
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(
                self, "Could not create account",
                f"The account was not created:\n\n{e}",
            )
            return
        self._refresh()
        # Tell the register window to refresh its sidebar too.
        owner = self.parent()
        reload_sidebar = getattr(owner, "_reload_sidebar", None)
        if callable(reload_sidebar):
            reload_sidebar(None)

    # ── formatting ──

    @staticmethod
    def _format(amount: Decimal) -> str:
        return f"£{amount:,.2f}"

    @staticmethod
    def _format_signed(amount: Decimal) -> str:
        if amount < 0:
            return f"-£{abs(amount):,.2f}"
        return f"£{amount:,.2f}"
