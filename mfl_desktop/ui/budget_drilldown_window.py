"""Budget Actual drill-down (ADR-058) — double-click an Actual cell in the
matrix to see exactly the transactions that make it up.

The set is computed from the *same* perimeter bucketing the matrix uses
(nearest-budgeted-ancestor, transfer cancellation, the Unbudgeted case), so the
list reconciles precisely with the cell — invaluable for validating the budget
and for tidying categories. The rows are an **editable** register (same
typeahead delegates as the main register), so recategorising a transaction here
flows straight back through the Repository; the matrix picks it up on its next
activation refresh.

The window is handed a fixed set of transaction ids + a title + the exact net
(from the perimeter txns), and shows just those rows. It deliberately has no
period/category controls of its own — it is a snapshot of one cell.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.delegates import (
    CategoryTypeaheadDelegate,
    PayeeTypeaheadDelegate,
    StatusDelegate,
)
from mfl_desktop.ui.filter_proxy import TransactionFilterProxy
from mfl_desktop.ui.register_model import TransactionTableModel
from mfl_desktop.ui import tokens

_COLUMN_WIDTHS = {
    "posted_date": 110, "account_name": 170, "payee_name": 210,
    "category_name": 200, "status": 100, "memo": 260, "amount": 110,
    "running_balance": 130,
}


class _TxnIdFilterProxy(TransactionFilterProxy):
    """Accept only rows whose transaction id is in a fixed set."""

    def __init__(self, source: TransactionTableModel) -> None:
        super().__init__(source)
        self._ids: set[int] = set()

    def set_ids(self, ids: set[int]) -> None:
        self._ids = set(ids)
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        if not super().filterAcceptsRow(source_row, parent):
            return False
        return self.sourceModel().row_at(source_row).id in self._ids


class BudgetDrillDownWindow(QMainWindow):
    """A snapshot register of the transactions behind one budget Actual cell."""

    def __init__(
        self,
        repo: Repository,
        *,
        txn_ids: set[int],
        title: str,
        net: Decimal,
        display_ccy: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle(title)
        self.resize(1040, 600)

        heading = QLabel(title)
        heading.setStyleSheet("font-size: 17px; font-weight: bold; padding: 2px;")

        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)

        self._model = TransactionTableModel(repo, account_id=None)
        self._proxy = _TxnIdFilterProxy(self._model)
        self._table.setModel(self._proxy)
        self._model.reload()
        self._proxy.set_ids(txn_ids)
        self._attach_delegates()
        self._apply_column_widths()

        count = len(txn_ids)
        footer = QLabel(
            f"{count} transaction" + ("s" if count != 1 else "")
            + f"  ·  net {display_ccy} {net:,.2f}"
        )
        tokens.themed(footer, "color: {muted_strong}; padding: 8px 4px;")

        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(14, 12, 14, 10)
        v.setSpacing(8)
        v.addWidget(heading)
        v.addWidget(self._table, stretch=1)
        v.addWidget(footer)
        self.setCentralWidget(container)

    def _attach_delegates(self) -> None:
        col_index = {
            name: i for i, (_, name, _) in enumerate(self._model.COLUMNS)
        }
        if "payee_name" in col_index:
            self._table.setItemDelegateForColumn(
                col_index["payee_name"],
                PayeeTypeaheadDelegate(self._repo, self._table),
            )
        if "category_name" in col_index:
            self._table.setItemDelegateForColumn(
                col_index["category_name"],
                CategoryTypeaheadDelegate(
                    self._repo,
                    on_create_category=self._on_create_category_inline,
                    parent=self._table,
                ),
            )
        if "status" in col_index:
            self._table.setItemDelegateForColumn(
                col_index["status"], StatusDelegate(self._table),
            )

    def _on_create_category_inline(self, name: str) -> Optional[int]:
        clean = (name or "").strip()
        if not clean:
            return None
        if QMessageBox.question(
            self, "Create category?",
            f"No category named {clean!r} exists.\n\n"
            f"Create it as a new top-level expense category?",
        ) != QMessageBox.Yes:
            return None
        try:
            return self._repo.create_category(
                name=clean, parent_id=None, kind="expense", source="user",
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not create category", str(e))
            return None

    def _apply_column_widths(self) -> None:
        col_index = {
            name: i for i, (_, name, _) in enumerate(self._model.COLUMNS)
        }
        for name, width in _COLUMN_WIDTHS.items():
            if name in col_index:
                self._table.setColumnWidth(col_index[name], width)
        if "memo" in col_index:
            self._table.horizontalHeader().setSectionResizeMode(
                col_index["memo"], QHeaderView.Stretch,
            )
