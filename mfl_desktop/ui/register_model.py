"""QAbstractTableModel for the register, backed by the Repository.

Loads the account's transactions into memory at construction time (Banktivity-
style — the dataset is personal-finance scale, tens of thousands at most).
Inline edits route through the Repository, then update the in-memory row so
the view repaints from the same source of truth.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from mfl_desktop.db.repository import Repository, TransactionRow

# Custom Qt role for "give me the underlying ID, not the display string."
# Used by the category delegate to read the current category_id from a cell.
ID_ROLE = Qt.UserRole + 1


class TransactionTableModel(QAbstractTableModel):
    """Backs the register table. Two column layouts:

    - Single-account (account_id is set): Date / Payee / Category / Status /
      Memo / Amount / Balance.
    - All-transactions (account_id is None): Date / Account / Payee /
      Category / Status / Memo / Amount  — no Balance, because it isn't
      meaningful across accounts of different types and currencies.
    """

    # (header_label, attribute_name, editable)
    COLUMNS_SINGLE = [
        ("Date",     "posted_date",     False),
        ("Payee",    "payee_name",      True),
        ("Category", "category_name",   True),
        ("Status",   "status",          True),
        ("Memo",     "memo",            True),
        ("Amount",   "amount",          False),
        ("Balance",  "running_balance", False),
    ]
    COLUMNS_ALL = [
        ("Date",     "posted_date",     False),
        ("Account",  "account_name",    False),
        ("Payee",    "payee_name",      True),
        ("Category", "category_name",   True),
        ("Status",   "status",          True),
        ("Memo",     "memo",            True),
        ("Amount",   "amount",          False),
    ]

    def __init__(self, repo: Repository, account_id: int | None) -> None:
        super().__init__()
        self._repo = repo
        self._account_id = account_id
        self.COLUMNS = (
            self.COLUMNS_SINGLE if account_id is not None else self.COLUMNS_ALL
        )
        self._rows: list[TransactionRow] = []

    def reload(self) -> None:
        self.beginResetModel()
        if self._account_id is not None:
            self._rows = self._repo.list_transactions_for_account(self._account_id)
        else:
            self._rows = self._repo.list_all_transactions()
        self.endResetModel()

    def row_at(self, source_row: int) -> TransactionRow:
        return self._rows[source_row]

    # ── required overrides ──

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return self.COLUMNS[section][0]

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

        # Underlying ID — used by delegates that pick by id (category, status).
        if role == ID_ROLE:
            if col_name == "category_name":
                return row.category_id
            if col_name == "status":
                return row.status
            return None

        if role in (Qt.DisplayRole, Qt.EditRole):
            value = getattr(row, col_name)
            if col_name in ("amount", "running_balance"):
                return f"{value:,.2f}"
            return value or ""

        if role == Qt.TextAlignmentRole:
            if col_name in ("amount", "running_balance"):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)

        if role == Qt.ForegroundRole and col_name == "amount":
            return QColor("#1b8a3a") if row.amount >= 0 else QColor("#b3261e")

        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid() or role != Qt.EditRole:
            return False
        col_name = self.COLUMNS[index.column()][1]
        if not self.COLUMNS[index.column()][2]:
            return False

        row = self._rows[index.row()]
        updated = self._apply_edit(row, col_name, value)
        if updated is None:
            return False
        self._rows[index.row()] = updated
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
        return True

    # ── edit routing ──

    def _apply_edit(
        self, row: TransactionRow, col_name: str, value,
    ) -> Optional[TransactionRow]:
        if col_name == "payee_name":
            new_name = str(value).strip()
            payee_id, display = self._repo.update_transaction_payee(row.id, new_name)
            return replace(row, payee_id=payee_id, payee_name=display)

        if col_name == "category_name":
            category_id = int(value)
            new_name = self._repo.update_transaction_category(row.id, category_id)
            return replace(row, category_id=category_id, category_name=new_name)

        if col_name == "status":
            new_status = str(value)
            self._repo.update_transaction_status(row.id, new_status)
            return replace(row, status=new_status)

        if col_name == "memo":
            new_memo = str(value)
            self._repo.update_transaction_memo(row.id, new_memo)
            return replace(row, memo=new_memo)

        return None
