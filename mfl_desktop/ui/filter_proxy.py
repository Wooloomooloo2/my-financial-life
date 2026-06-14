"""Filter / sort proxy for the register.

Filters on free-text search (payee, memo, amount, date, and — for investment
rows — security symbol + name), status, and category id. Sorts on the
underlying value of each column so amounts sort numerically and dates
chronologically — the source model's formatted strings are not what gets
compared.

Amount search is comma-insensitive: typing "3250" or "3,250" both match a
3,250.00 transaction. Both signed and absolute forms of the amount are
in the haystack so the user doesn't have to think about direction (a
search for "3250" finds the £3,250 receipt *and* the -£3,250 payment).
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
        # Strip commas so "3,250" and "3250" both produce the same needle —
        # the haystack is built without commas too (see filterAcceptsRow).
        self._search = text.lower().strip().replace(",", "")
        self.invalidateRowsFilter()

    def set_status(self, status: str) -> None:
        # "All" = no filter; "Unreconciled" = every status except Reconciled
        # (Pending / Uncleared / Cleared); anything else = exact status match.
        self._status = status
        self.invalidateRowsFilter()

    def set_category_id(self, category_id: Optional[int]) -> None:
        self._category_id = category_id
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        model: TransactionTableModel = self.sourceModel()
        row = model.row_at(source_row)
        if self._status == "Unreconciled":
            if row.status == "Reconciled":
                return False
        elif self._status != "All" and row.status != self._status:
            return False
        if self._category_id is not None:
            # A split parent's own category_id is Uncategorised; match it when
            # the filtered category is on one of its lines (ADR-051) so
            # filtering by a split-line category surfaces the "—Split—" row.
            if (
                row.category_id != self._category_id
                and self._category_id not in row.split_category_ids
            ):
                return False
        if self._search:
            # ADR-061: the haystack (payee/memo/date/security/both amount forms)
            # is precomputed once per row by the model, so each keystroke is a
            # single substring test rather than rebuilding seven fields + two
            # amount formats for every loaded row.
            if self._search not in model.search_blob_at(source_row):
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
