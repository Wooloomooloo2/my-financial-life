"""Account sidebar — 'All transactions' on top, then accounts.

Emits `selection_changed` with the selected account IRI, or None when
'All transactions' is selected.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from mfl_desktop.db.repository import AccountSummary

_ALL_SENTINEL = "__all__"


class AccountSidebar(QListWidget):
    selection_changed = Signal(object)  # str | None

    def __init__(self, accounts: list[AccountSummary], parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(200)
        self.setMaximumWidth(320)
        self._populate(accounts)
        self.currentItemChanged.connect(self._on_change)

    def reload(self, accounts: list[AccountSummary]) -> None:
        """Rebuild the list, preserving the current selection if possible."""
        current_data = (
            self.currentItem().data(Qt.UserRole)
            if self.currentItem() is not None
            else None
        )
        self.blockSignals(True)
        self.clear()
        self._populate(accounts)
        restored = False
        for i in range(self.count()):
            if self.item(i).data(Qt.UserRole) == current_data:
                self.setCurrentRow(i)
                restored = True
                break
        if not restored:
            self.setCurrentRow(0)
        self.blockSignals(False)

    def _populate(self, accounts: list[AccountSummary]) -> None:
        all_item = QListWidgetItem("All transactions")
        all_item.setData(Qt.UserRole, _ALL_SENTINEL)
        font = all_item.font()
        font.setBold(True)
        all_item.setFont(font)
        self.addItem(all_item)

        for acct in accounts:
            item = QListWidgetItem(acct.name)
            item.setData(Qt.UserRole, acct.iri)
            item.setToolTip(
                f"{acct.iri}\nType: {acct.type}\nCurrency: {acct.currency}"
            )
            self.addItem(item)

        self.setCurrentRow(0)

    def _on_change(self, current, previous) -> None:
        if current is None:
            return
        data = current.data(Qt.UserRole)
        self.selection_changed.emit(None if data == _ALL_SENTINEL else data)

    def current_selection(self) -> Optional[str]:
        item = self.currentItem()
        if item is None:
            return None
        data = item.data(Qt.UserRole)
        return None if data == _ALL_SENTINEL else data
