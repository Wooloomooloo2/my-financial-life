"""New Report… dialog (ADR-039).

Sidebar context-menu entry-point for creating a saved report. The user
picks the report *type*; the dialog returns the type key (one of
:data:`mfl_desktop.reports.filters.REPORT_TYPES`) and the caller opens
the matching report window in its "bare" form. The first Save from
inside that window then performs the actual ``create_report`` call.

Round 1 ships Spending Over Time only; the other three types are listed
as disabled options so the menu's shape is settled before the per-type
windows arrive (clicking a disabled row is a no-op).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from mfl_desktop.reports.filters import (
    REPORT_TYPE_LABELS,
    TYPE_CATEGORY_PAYEE,
    TYPE_INCOME_EXPENSE,
    TYPE_INVESTMENT_RETURNS,
    TYPE_NET_WORTH,
    TYPE_PAYEE,
    TYPE_SANKEY,
    TYPE_SPENDING_OVER_TIME,
)

# Types shipped with persistence today. The rest render but are disabled
# until their per-type ADR lands (ADR-039 §rounds).
_AVAILABLE_TYPES: tuple[str, ...] = (
    TYPE_SPENDING_OVER_TIME, TYPE_INCOME_EXPENSE, TYPE_INVESTMENT_RETURNS,
    TYPE_SANKEY, TYPE_PAYEE, TYPE_CATEGORY_PAYEE,
)
_TYPE_ORDER: tuple[str, ...] = (
    TYPE_SPENDING_OVER_TIME,
    TYPE_INCOME_EXPENSE,
    TYPE_PAYEE,
    TYPE_CATEGORY_PAYEE,
    TYPE_INVESTMENT_RETURNS,
    TYPE_SANKEY,
    TYPE_NET_WORTH,
)


class NewReportDialog(QDialog):
    """Modal type picker. ``values()`` returns the chosen type key (or
    ``None`` on Cancel)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New report")
        self.setModal(True)
        self._chosen_type: Optional[str] = None

        self._list = QListWidget()
        for type_key in _TYPE_ORDER:
            label = REPORT_TYPE_LABELS.get(type_key, type_key)
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, type_key)
            if type_key not in _AVAILABLE_TYPES:
                # Disabled rows render greyed out — Qt's standard handling.
                item.setFlags(Qt.NoItemFlags)
                item.setText(f"{label}  (coming later)")
            self._list.addItem(item)
        # Default selection: the first available row.
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.flags() & Qt.ItemIsSelectable:
                self._list.setCurrentRow(i)
                break
        self._list.itemDoubleClicked.connect(self._on_accept)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(self._list)
        root.addWidget(buttons)
        self.resize(320, 260)

    def values(self) -> Optional[str]:
        return self._chosen_type

    def _on_accept(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        if not (item.flags() & Qt.ItemIsSelectable):
            return
        self._chosen_type = item.data(Qt.UserRole)
        self.accept()
