"""Account sidebar — 'All transactions' on top, then folders + accounts.

Banktivity-style hierarchy: an explicit "All transactions" row sits at
the very top; below it are folders (collapsible) and root accounts
(those not in a folder). Each row shows a balance — accounts show their
own; folders show the sum of their members.

In v1 (ADR-015), folders are *not* directly selectable: clicking a folder
row simply toggles its expansion. The user must click an account to
change the register view. This keeps the sidebar's "what's currently
shown in the register" contract identical to the old flat list — only
accounts (and All transactions) produce `selection_changed` emissions.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHeaderView, QTreeWidget, QTreeWidgetItem

from mfl_desktop.db.repository import AccountSummary, FolderSummary

_ALL_SENTINEL = "__all__"

# Custom data role for the kind of row: "all" | "folder" | "account".
KIND_ROLE = Qt.UserRole + 1

# A small list of currency symbols we render in front of balances. Anything
# not in here falls back to no symbol — the user still sees the signed
# decimal so nothing is hidden.
_CURRENCY_SYMBOLS: dict[str, str] = {
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
    "JPY": "¥",
}


class AccountSidebar(QTreeWidget):
    """Two-column tree (Name | Balance), header hidden.

    Emits `selection_changed(object)`:
        - str   when an account row is selected (the account's IRI),
        - None  when 'All transactions' is selected.
    Folder selections do not emit; folders are display/grouping only.
    """

    selection_changed = Signal(object)

    def __init__(
        self,
        accounts: list[AccountSummary],
        folders: list[FolderSummary],
        balances: dict[int, Decimal],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.setMaximumWidth(540)
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setIndentation(14)
        self.setUniformRowHeights(True)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)

        self._populate(accounts, folders, balances)
        self.itemSelectionChanged.connect(self._on_selection_changed)
        self.itemClicked.connect(self._on_item_clicked)

    def reload(
        self,
        accounts: list[AccountSummary],
        folders: list[FolderSummary],
        balances: dict[int, Decimal],
    ) -> None:
        """Rebuild the tree, preserving the current account selection if it
        still exists. Otherwise fall back to 'All transactions'."""
        prev_iri = self.current_selection()
        self.blockSignals(True)
        self.clear()
        self._populate(accounts, folders, balances)
        if prev_iri is None or not self.select_account_by_iri(prev_iri):
            top = self.topLevelItem(0)
            if top is not None:
                self.setCurrentItem(top)
        self.blockSignals(False)

    # ── population ──

    def _populate(
        self,
        accounts: list[AccountSummary],
        folders: list[FolderSummary],
        balances: dict[int, Decimal],
    ) -> None:
        # 1. 'All transactions' at the top.
        all_item = QTreeWidgetItem(["All transactions", ""])
        all_item.setData(0, Qt.UserRole, _ALL_SENTINEL)
        all_item.setData(0, KIND_ROLE, "all")
        font = all_item.font(0)
        font.setBold(True)
        all_item.setFont(0, font)
        self.addTopLevelItem(all_item)

        # 2. Folders + their accounts, in folder sort_order.
        accounts_by_folder: dict[Optional[int], list[AccountSummary]] = {}
        for a in accounts:
            accounts_by_folder.setdefault(a.folder_id, []).append(a)
        for siblings in accounts_by_folder.values():
            siblings.sort(key=lambda a: (a.family, a.name.lower()))

        for f in folders:
            members = accounts_by_folder.get(f.id, [])
            folder_sum = sum(
                (balances.get(a.id, Decimal("0.00")) for a in members),
                start=Decimal("0.00"),
            )
            # Show the currency symbol on the folder sum only when all the
            # member accounts share one currency — mixed-currency sums are
            # arithmetically naive (ADR-015) and a single symbol would
            # mislead in that case.
            currencies = {a.currency for a in members}
            folder_currency = next(iter(currencies)) if len(currencies) == 1 else None
            folder_item = QTreeWidgetItem(
                [f.name, self._format(folder_sum, folder_currency)]
            )
            folder_item.setData(0, Qt.UserRole, f.id)
            folder_item.setData(0, KIND_ROLE, "folder")
            # Folders are visible and clickable (we toggle expansion on
            # click) but not directly selectable — see class docstring.
            folder_item.setFlags(Qt.ItemIsEnabled)
            font = folder_item.font(0)
            font.setBold(True)
            folder_item.setFont(0, font)
            folder_item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            self.addTopLevelItem(folder_item)
            for a in members:
                folder_item.addChild(self._make_account_item(a, balances))
            folder_item.setExpanded(True)

        # 3. Root accounts (no folder), after the folders.
        for a in sorted(
            accounts_by_folder.get(None, []),
            key=lambda a: (a.family, a.name.lower()),
        ):
            self.addTopLevelItem(self._make_account_item(a, balances))

        top = self.topLevelItem(0)
        if top is not None:
            self.setCurrentItem(top)

    def _make_account_item(
        self, account: AccountSummary, balances: dict[int, Decimal],
    ) -> QTreeWidgetItem:
        bal = balances.get(account.id, Decimal("0.00"))
        item = QTreeWidgetItem(
            [account.name, self._format(bal, account.currency)]
        )
        item.setData(0, Qt.UserRole, account.iri)
        item.setData(0, KIND_ROLE, "account")
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        item.setToolTip(
            0,
            f"{account.iri}\nType: {account.type}\nCurrency: {account.currency}",
        )
        return item

    @staticmethod
    def _format(amount: Decimal, currency: Optional[str]) -> str:
        """Signed, two decimals, with the currency symbol when we know it.
        Negative values are rendered with a leading minus *outside* the
        symbol ('-£40.00'), matching how the user reads bank statements."""
        body = f"{abs(amount):,.2f}"
        symbol = _CURRENCY_SYMBOLS.get(currency) if currency else None
        if symbol is None:
            return f"-{body}" if amount < 0 else body
        return f"-{symbol}{body}" if amount < 0 else f"{symbol}{body}"

    # ── signals / event handling ──

    def _on_selection_changed(self) -> None:
        """itemSelectionChanged fires only when a selectable row is picked
        (accounts or 'All transactions') — folders aren't selectable so
        they don't reach this handler. If the selection set is empty
        (a folder click that cleared selection) we keep quiet."""
        items = self.selectedItems()
        if not items:
            return
        item = items[0]
        kind = item.data(0, KIND_ROLE)
        if kind == "all":
            self.selection_changed.emit(None)
        elif kind == "account":
            self.selection_changed.emit(item.data(0, Qt.UserRole))

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """Click on a folder row toggles its expansion. Lets the user
        expand/collapse without having to hit the small disclosure
        triangle exactly."""
        if item is not None and item.data(0, KIND_ROLE) == "folder":
            item.setExpanded(not item.isExpanded())

    # ── public helpers used by RegisterWindow ──

    def current_selection(self) -> Optional[str]:
        """Returns the IRI of the currently-selected account, or None if
        'All transactions' is selected. None is also returned if nothing
        is selected (e.g. after a folder click cleared selection)."""
        items = self.selectedItems()
        if not items:
            return None
        item = items[0]
        kind = item.data(0, KIND_ROLE)
        if kind == "account":
            return item.data(0, Qt.UserRole)
        return None

    def select_account_by_iri(self, iri: str) -> bool:
        """Find the account anywhere in the tree (root or inside a folder)
        and make it current. Returns True if found."""
        for i in range(self.topLevelItemCount()):
            top = self.topLevelItem(i)
            if top.data(0, KIND_ROLE) == "account" and top.data(0, Qt.UserRole) == iri:
                self.setCurrentItem(top)
                return True
            if top.data(0, KIND_ROLE) == "folder":
                for j in range(top.childCount()):
                    child = top.child(j)
                    if child.data(0, KIND_ROLE) == "account" and child.data(0, Qt.UserRole) == iri:
                        self.setCurrentItem(child)
                        return True
        return False

    def select_all_transactions(self) -> None:
        top = self.topLevelItem(0)
        if top is not None:
            self.setCurrentItem(top)
