"""Filter / sort proxy for the register.

Filters on free-text search (payee + memo), status, and category id. Sorts on
the underlying value of each column so amounts sort numerically and dates
chronologically — the source model's formatted strings are not what gets
compared.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel

from mfl_desktop.ui.register_model import TransactionTableModel


class TransactionFilterProxy(QSortFilterProxyModel):
    def __init__(self, source: TransactionTableModel) -> None:
        super().__init__()
        self.setSourceModel(source)
        self._search = ""
        self._status = "All"
        self._category_id: Optional[int] = None

    def set_search(self, text: str) -> None:
        self._search = text.lower().strip()
        self.invalidateRowsFilter()

    def set_status(self, status: str) -> None:
        self._status = status
        self.invalidateRowsFilter()

    def set_category_id(self, category_id: Optional[int]) -> None:
        self._category_id = category_id
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        model: TransactionTableModel = self.sourceModel()
        row = model.row_at(source_row)
        if self._status != "All" and row.status != self._status:
            return False
        if self._category_id is not None and row.category_id != self._category_id:
            return False
        if self._search:
            haystack = " ".join(filter(None, [row.payee_name, row.memo])).lower()
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
