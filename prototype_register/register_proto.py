"""Prototype register window — PySide6 + SQLite.

Purpose: de-risk the most uncertain piece of the desktop rewrite (ADR-008) —
does a native QTableView over a SQLite-backed repository give the register
feel that HTMX could not? Run it, scroll the 10k rows, edit a few cells,
type in the search box, switch categories. If it feels right, the rest of
the rewrite follows the same layering.

Architecture (intentional — mirrors what the real app will use):

    QMainWindow  ──►  QSortFilterProxyModel  ──►  TransactionTableModel  ──►  Repository  ──►  SQLite
       (UI)              (filter / sort)           (Qt model contract)        (only file that knows SQL)

Run:
    pip install -r requirements.txt
    python seed.py
    python register_proto.py
"""
from __future__ import annotations

import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QStatusBar,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

DB_PATH = Path(__file__).parent / "prototype.db"

CATEGORIES = [
    "Uncategorised",
    "Charity and gifts", "Childcare", "Dining out", "Education", "Groceries",
    "Healthcare", "Holidays and travel", "Housing", "Insurance",
    "Investment income", "Other expense", "Rental income", "Salary",
    "Shopping", "Subscriptions", "Transport", "Utilities",
]
STATUSES = ["Pending", "Uncleared", "Cleared", "Reconciled"]


# ─────────────────────────────────────────────────────────────────────────────
# Repository — only place that touches SQL
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Transaction:
    id: int
    iri: str
    posted_date: str  # ISO 8601 — sorts lexicographically as chronological
    amount: float
    payee: Optional[str]
    category: Optional[str]
    status: str
    memo: Optional[str]
    running_balance: float = 0.0  # computed at load time, not persisted


class Repository:
    EDITABLE_FIELDS = frozenset({"payee", "category", "status", "memo"})

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row

    def first_account_id(self) -> int:
        row = self._conn.execute("SELECT id FROM account LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("no accounts in database — run seed.py first")
        return row["id"]

    def account_name(self, account_id: int) -> str:
        return self._conn.execute(
            "SELECT name FROM account WHERE id = ?", (account_id,)
        ).fetchone()["name"]

    def load_account_transactions(self, account_id: int) -> list[Transaction]:
        cur = self._conn.execute(
            "SELECT id, iri, posted_date, amount, payee, category, status, memo "
            "FROM txn WHERE account_id = ? ORDER BY posted_date ASC, id ASC",
            (account_id,),
        )
        rows: list[Transaction] = []
        running = 0.0
        for r in cur:
            running += r["amount"]
            rows.append(Transaction(
                id=r["id"], iri=r["iri"], posted_date=r["posted_date"],
                amount=r["amount"], payee=r["payee"], category=r["category"],
                status=r["status"], memo=r["memo"], running_balance=running,
            ))
        return rows

    def update_field(self, txn_id: int, field_name: str, value) -> None:
        if field_name not in self.EDITABLE_FIELDS:
            raise ValueError(f"field {field_name!r} is not editable")
        self._conn.execute(
            f"UPDATE txn SET {field_name} = ? WHERE id = ?", (value, txn_id),
        )
        self._conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Table model — pure Qt, holds the in-memory snapshot of rows
# ─────────────────────────────────────────────────────────────────────────────


class TransactionTableModel(QAbstractTableModel):
    # (header_label, attribute_name, editable)
    COLUMNS = [
        ("Date",     "posted_date",     False),
        ("Payee",    "payee",           True),
        ("Category", "category",        True),
        ("Status",   "status",          True),
        ("Memo",     "memo",            True),
        ("Amount",   "amount",          False),
        ("Balance",  "running_balance", False),
    ]

    def __init__(self, repo: Repository, account_id: int) -> None:
        super().__init__()
        self._repo = repo
        self._account_id = account_id
        self._rows: list[Transaction] = []

    def reload(self) -> int:
        self.beginResetModel()
        t0 = time.perf_counter()
        self._rows = self._repo.load_account_transactions(self._account_id)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        self.endResetModel()
        return elapsed_ms

    def row_at(self, source_row: int) -> Transaction:
        return self._rows[source_row]

    # ── required overrides ──

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.COLUMNS[section][0]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if self.COLUMNS[index.column()][2]:
            f |= Qt.ItemIsEditable
        return f

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col_name = self.COLUMNS[index.column()][1]
        value = getattr(row, col_name)

        if role in (Qt.DisplayRole, Qt.EditRole):
            if col_name in {"amount", "running_balance"}:
                return f"{value:,.2f}"
            return value or ""

        if role == Qt.TextAlignmentRole:
            if col_name in {"amount", "running_balance"}:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)

        if role == Qt.ForegroundRole and col_name == "amount":
            return QColor("#1b8a3a") if row.amount >= 0 else QColor("#b3261e")

        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if role != Qt.EditRole or not index.isValid():
            return False
        col_name = self.COLUMNS[index.column()][1]
        if not self.COLUMNS[index.column()][2]:
            return False
        row = self._rows[index.row()]
        self._repo.update_field(row.id, col_name, value)
        self._rows[index.row()] = replace(row, **{col_name: value})
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Filter / sort proxy
# ─────────────────────────────────────────────────────────────────────────────


class TransactionFilterProxy(QSortFilterProxyModel):
    """Filters on search/status/category and sorts on the underlying value
    (not the formatted display string) so '100' > '9' rather than the reverse."""

    def __init__(self, source: TransactionTableModel) -> None:
        super().__init__()
        self.setSourceModel(source)
        self._search = ""
        self._status = "All"
        self._category = "All"

    def set_search(self, text: str) -> None:
        self._search = text.lower().strip()
        self.invalidateRowsFilter()

    def set_status(self, status: str) -> None:
        self._status = status
        self.invalidateRowsFilter()

    def set_category(self, category: str) -> None:
        self._category = category
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        model: TransactionTableModel = self.sourceModel()
        row = model.row_at(source_row)
        if self._status != "All" and row.status != self._status:
            return False
        if self._category != "All" and (row.category or "Uncategorised") != self._category:
            return False
        if self._search:
            haystack = " ".join(filter(None, [row.payee, row.memo])).lower()
            if self._search not in haystack:
                return False
        return True

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        model: TransactionTableModel = self.sourceModel()
        col_name = model.COLUMNS[left.column()][1]
        lv = getattr(model.row_at(left.row()), col_name)
        rv = getattr(model.row_at(right.row()), col_name)
        if lv is None and rv is None:
            return False
        if lv is None:
            return True
        if rv is None:
            return False
        return lv < rv


# ─────────────────────────────────────────────────────────────────────────────
# Combo-box delegate for category / status columns
# ─────────────────────────────────────────────────────────────────────────────


class ComboBoxDelegate(QStyledItemDelegate):
    def __init__(self, choices: list[str], parent=None) -> None:
        super().__init__(parent)
        self._choices = choices

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.addItems(self._choices)
        # Commit + close on selection, so Qt doesn't try a second commit when
        # the dropdown loses focus (the "editor does not belong to this view"
        # warning otherwise fires from that stale second commit).
        combo.activated.connect(lambda _: self._commit_and_close(combo))
        return combo

    def _commit_and_close(self, editor: QComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QComboBox, index):
        current = index.data(Qt.EditRole) or ""
        editor.blockSignals(True)
        i = editor.findText(current)
        if i >= 0:
            editor.setCurrentIndex(i)
        editor.blockSignals(False)
        editor.showPopup()

    def setModelData(self, editor: QComboBox, model, index):
        model.setData(index, editor.currentText(), Qt.EditRole)


# ─────────────────────────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────────────────────────


class RegisterWindow(QMainWindow):
    def __init__(self, repo: Repository, account_id: int) -> None:
        super().__init__()
        self._repo = repo
        self.setWindowTitle(
            f"My Financial Life — Register prototype  ·  {repo.account_name(account_id)}"
        )
        self.resize(1280, 760)

        self._model = TransactionTableModel(repo, account_id)
        self._proxy = TransactionFilterProxy(self._model)

        # filter bar
        search = QLineEdit()
        search.setPlaceholderText("Search payee or memo…")
        search.textChanged.connect(self._proxy.set_search)

        status_combo = QComboBox()
        status_combo.addItems(["All", *STATUSES])
        status_combo.currentTextChanged.connect(self._proxy.set_status)

        category_combo = QComboBox()
        category_combo.addItems(["All", *CATEGORIES])
        category_combo.currentTextChanged.connect(self._proxy.set_category)

        filter_bar = QHBoxLayout()
        filter_bar.setContentsMargins(8, 8, 8, 4)
        filter_bar.addWidget(QLabel("Search:"))
        filter_bar.addWidget(search, stretch=2)
        filter_bar.addSpacing(12)
        filter_bar.addWidget(QLabel("Status:"))
        filter_bar.addWidget(status_combo)
        filter_bar.addSpacing(12)
        filter_bar.addWidget(QLabel("Category:"))
        filter_bar.addWidget(category_combo)

        # table
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setEditTriggers(
            QTableView.DoubleClicked
            | QTableView.SelectedClicked
            | QTableView.EditKeyPressed
        )
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(22)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.horizontalHeader().setStretchLastSection(False)

        col_index = {name: i for i, (_, name, _) in enumerate(self._model.COLUMNS)}
        self._table.setItemDelegateForColumn(
            col_index["category"], ComboBoxDelegate(CATEGORIES, self._table),
        )
        self._table.setItemDelegateForColumn(
            col_index["status"], ComboBoxDelegate(STATUSES, self._table),
        )

        # layout
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(filter_bar)
        layout.addWidget(self._table)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar(self))
        self._initial_load_ms = 0

        # Load before wiring status-bar signals: reload() emits modelReset,
        # which would otherwise call _update_status before the table has any
        # state to report.
        self._initial_load_ms = self._model.reload()
        self._set_default_column_widths()
        self._table.sortByColumn(
            col_index["posted_date"], Qt.DescendingOrder
        )  # newest first, the natural register order

        self._proxy.layoutChanged.connect(self._update_status)
        self._proxy.modelReset.connect(self._update_status)
        self._proxy.rowsInserted.connect(self._update_status)
        self._proxy.rowsRemoved.connect(self._update_status)

        self._update_status()

    def _set_default_column_widths(self) -> None:
        widths = [110, 220, 200, 110, 280, 110, 130]
        for i, w in enumerate(widths):
            self._table.setColumnWidth(i, w)

    def _update_status(self) -> None:
        visible = self._proxy.rowCount()
        total = self._model.rowCount()
        self.statusBar().showMessage(
            f"Showing {visible:,} of {total:,} transactions  ·  "
            f"initial load {self._initial_load_ms} ms"
        )


def main() -> int:
    if not DB_PATH.exists():
        print(
            f"Database not found at {DB_PATH}.\nRun `python seed.py` first.",
            file=sys.stderr,
        )
        return 1
    app = QApplication(sys.argv)
    repo = Repository(DB_PATH)
    win = RegisterWindow(repo, repo.first_account_id())
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
