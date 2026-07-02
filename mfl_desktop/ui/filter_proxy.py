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

from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel

from mfl_desktop import txn_status
from mfl_desktop.ui.register_model import TransactionTableModel


class TransactionFilterProxy(QSortFilterProxyModel):
    def __init__(self, source: TransactionTableModel) -> None:
        super().__init__()
        self.setSourceModel(source)
        self._search = ""
        self._status = "All"
        self._category_id: Optional[int] = None
        # ADR-062: optional date range (inclusive 'YYYY-MM-DD' strings — bare
        # lexicographic compare, no parsing) and signed-amount range. None on
        # either end means unbounded there.
        self._date_from: Optional[str] = None
        self._date_to: Optional[str] = None
        self._amt_min: Optional[Decimal] = None
        self._amt_max: Optional[Decimal] = None

    def set_search(self, text: str) -> None:
        # Strip commas so "3,250" and "3250" both produce the same needle —
        # the haystack is built without commas too (see filterAcceptsRow).
        self._search = text.lower().strip().replace(",", "")
        self.invalidateRowsFilter()

    def set_status(self, status: str) -> None:
        # "All" = no filter; "Unreconciled" = every status except reconciled
        # (pending / cleared / matched); anything else is a status *label*
        # (ADR-130) matched exactly against the row's stored key.
        self._status = status
        self.invalidateRowsFilter()

    def set_category_id(self, category_id: Optional[int]) -> None:
        self._category_id = category_id
        self.invalidateRowsFilter()

    def set_date_range(
        self, date_from: Optional[str], date_to: Optional[str],
    ) -> None:
        """Inclusive 'YYYY-MM-DD' bounds (either None = unbounded)."""
        self._date_from = date_from
        self._date_to = date_to
        self.invalidateRowsFilter()

    def set_amount_range(
        self, amount_min: Optional[Decimal], amount_max: Optional[Decimal],
    ) -> None:
        """Inclusive signed-amount bounds (either None = unbounded)."""
        self._amt_min = amount_min
        self._amt_max = amount_max
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        model: TransactionTableModel = self.sourceModel()
        row = model.row_at(source_row)
        if self._status == "Unreconciled":
            if row.status == txn_status.RECONCILED:
                return False
        elif self._status != "All" and row.status != txn_status.key_for_label(self._status):
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
        # ADR-062: date range — lexicographic on the stored ISO date, inclusive.
        if self._date_from is not None and row.posted_date < self._date_from:
            return False
        if self._date_to is not None and row.posted_date > self._date_to:
            return False
        # ADR-062: signed-amount range, inclusive.
        if self._amt_min is not None and row.amount < self._amt_min:
            return False
        if self._amt_max is not None and row.amount > self._amt_max:
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
