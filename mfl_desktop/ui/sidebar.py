"""Sidebar — two sections (Accounts on top, Reports below), Banktivity-style.

A single ``QTreeWidget`` carries two top-level "group" rows ("Accounts"
and "Reports") that are non-selectable, always-expanded headers separated
by a thin slate-200 underline on the lower section header (ADR-039
§sidebar-layout). Folders and leaf rows live underneath their group; both
sections support folders + root leaves.

In v1 (ADR-015), account folders are *not* directly selectable: clicking
a folder row simply toggles its expansion. The same rule applies to the
new report folders. Selection emits on accounts, reports, and the
'All transactions' row only.

Selection signal — ``selection_changed(kind, payload)``:

- ``("all_transactions", None)``  → 'All transactions' picked
- ``("account", account_iri)``    → an account row picked
- ``("report", report_id)``       → a saved report picked

The signal carries the discriminator so the register window can dispatch
without re-walking the tree (ADR-039 §sidebar-restructure). The previous
single-arg shape from ADR-015 is replaced — one breaking change for one
caller.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QHeaderView, QTreeWidget, QTreeWidgetItem

from mfl_desktop.db.repository import (
    AccountSummary, FolderSummary, ReportFolderRow, Repository, ReportRow,
)

_ALL_SENTINEL = "__all__"

# Custom data role for the kind of row.
# Values:
#   "section_accounts" | "section_reports"  — non-selectable group headers
#   "all"                                   — All transactions row
#   "folder"                                — account folder
#   "closed_group"                          — 'Closed accounts' grouping row (ADR-069)
#   "account"                               — leaf account (open or closed)
#   "report_folder"                         — saved-reports folder
#   "report"                                — saved report row
KIND_ROLE = Qt.UserRole + 1

# Set True on a closed (archived) account leaf so callers (the register
# window's context menu) can offer Reopen instead of Edit/Delete (ADR-069).
CLOSED_ROLE = Qt.UserRole + 2

# Section header palette — slate-700 text on a slate-50 background. The
# REPORTS header gets a thin slate-200 top border via paintEvent (set on
# the item's data role and consumed by a small delegate hook below).
_HEADER_FG = QColor("#334155")    # slate-700
_HEADER_BG = QColor("#f8fafc")    # slate-50
_CLOSED_FG = QColor("#94a3b8")    # slate-400 — muted text for closed accounts

_CURRENCY_SYMBOLS: dict[str, str] = {
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
    "JPY": "¥",
}


class Sidebar(QTreeWidget):
    """Two-column tree (Name | Balance), header hidden.

    Emits ``selection_changed(kind, payload)`` for the three selectable
    row kinds (see module docstring). Folder + section-header rows do
    not emit; they're display/grouping only.
    """

    selection_changed = Signal(str, object)

    def __init__(
        self,
        accounts: list[AccountSummary],
        folders: list[FolderSummary],
        balances: dict[int, Decimal],
        reports: Optional[list[ReportRow]] = None,
        report_folders: Optional[list[ReportFolderRow]] = None,
        repo: Optional[Repository] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # The repo is used only to convert a *mixed-currency* folder's
        # members to one display currency for its roll-up total (ADR-055
        # follow-up — a bare cross-currency sum is the bug we fixed in Net
        # Worth). Repo-less callers (tests) fall back to the old no-symbol
        # bare sum for mixed folders; single-currency folders never convert.
        self._repo = repo
        self._display_currency = self._resolve_display_currency()
        self.setMinimumWidth(240)
        # No max width — the splitter handle is the constraint. The
        # earlier 540px cap left long account names ("Smile Current
        # Account") elided even when the user had dragged the splitter
        # wider, with the extra space appearing as dead whitespace
        # between the sidebar's right edge and the splitter handle.
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setIndentation(12)
        self.setUniformRowHeights(False)
        self.setTextElideMode(Qt.ElideRight)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setStretchLastSection(False)

        self._populate(accounts, folders, balances, reports or [], report_folders or [])
        self.itemSelectionChanged.connect(self._on_selection_changed)
        self.itemClicked.connect(self._on_item_clicked)

    def reload(
        self,
        accounts: list[AccountSummary],
        folders: list[FolderSummary],
        balances: dict[int, Decimal],
        reports: Optional[list[ReportRow]] = None,
        report_folders: Optional[list[ReportFolderRow]] = None,
    ) -> None:
        """Rebuild the tree, preserving the current selection if its target
        still exists. Falls back to 'All transactions' otherwise."""
        prev_kind, prev_payload = self.current_selection_tuple()
        self.blockSignals(True)
        self.clear()
        self._populate(accounts, folders, balances, reports or [], report_folders or [])
        if not self._restore_selection(prev_kind, prev_payload):
            self.select_all_transactions()
        self.blockSignals(False)

    # ── population ──

    def _populate(
        self,
        accounts: list[AccountSummary],
        folders: list[FolderSummary],
        balances: dict[int, Decimal],
        reports: list[ReportRow],
        report_folders: list[ReportFolderRow],
    ) -> None:
        # ── Accounts section ──
        accounts_header = self._make_section_header(
            "ACCOUNTS", "section_accounts", with_top_rule=False,
        )
        self.addTopLevelItem(accounts_header)
        # Section headers have no column-1 content; spanning gives the
        # header text the full width without an awkward right-edge gap.
        accounts_header.setFirstColumnSpanned(True)

        # 'All transactions' is always the first child of the Accounts header.
        all_item = QTreeWidgetItem(["All transactions", ""])
        all_item.setData(0, Qt.UserRole, _ALL_SENTINEL)
        all_item.setData(0, KIND_ROLE, "all")
        font = all_item.font(0)
        font.setBold(True)
        all_item.setFont(0, font)
        accounts_header.addChild(all_item)

        # Closed (archived) accounts are pulled out of the folder layout and
        # collected into a single collapsed 'Closed accounts' group at the
        # bottom of the section (ADR-069); active accounts keep their folders.
        active = [a for a in accounts if not a.is_closed]
        closed = [a for a in accounts if a.is_closed]

        accounts_by_folder: dict[Optional[int], list[AccountSummary]] = {}
        for a in active:
            accounts_by_folder.setdefault(a.folder_id, []).append(a)
        for siblings in accounts_by_folder.values():
            siblings.sort(key=lambda a: (a.family, a.name.lower()))

        for f in folders:
            members = accounts_by_folder.get(f.id, [])
            balance_text, balance_tip = self._folder_balance_cell(members, balances)
            folder_item = QTreeWidgetItem([f.name, balance_text])
            folder_item.setData(0, Qt.UserRole, f.id)
            folder_item.setData(0, KIND_ROLE, "folder")
            folder_item.setFlags(Qt.ItemIsEnabled)
            font = folder_item.font(0)
            font.setBold(True)
            folder_item.setFont(0, font)
            folder_item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if balance_tip:
                folder_item.setToolTip(0, balance_tip)
                folder_item.setToolTip(1, balance_tip)
            accounts_header.addChild(folder_item)
            for a in members:
                folder_item.addChild(self._make_account_item(a, balances))
            folder_item.setExpanded(True)

        for a in sorted(
            accounts_by_folder.get(None, []),
            key=lambda a: (a.family, a.name.lower()),
        ):
            accounts_header.addChild(self._make_account_item(a, balances))

        # 'Closed accounts' group — collapsed by default, only when present.
        if closed:
            label = f"Closed accounts ({len(closed)})"
            closed_group = QTreeWidgetItem([label, ""])
            closed_group.setData(0, KIND_ROLE, "closed_group")
            closed_group.setFlags(Qt.ItemIsEnabled)
            font = closed_group.font(0)
            font.setBold(True)
            closed_group.setFont(0, font)
            closed_group.setForeground(0, QBrush(_CLOSED_FG))
            closed_group.setToolTip(
                0,
                "Closed accounts are kept for history but excluded from Net "
                "Worth and account pickers. Right-click one to reopen it.",
            )
            accounts_header.addChild(closed_group)
            for a in sorted(closed, key=lambda a: (a.family, a.name.lower())):
                closed_group.addChild(
                    self._make_account_item(a, balances, is_closed=True)
                )
            closed_group.setExpanded(False)

        accounts_header.setExpanded(True)

        # ── Reports section ──
        reports_header = self._make_section_header(
            "REPORTS", "section_reports", with_top_rule=True,
        )
        self.addTopLevelItem(reports_header)
        reports_header.setFirstColumnSpanned(True)

        reports_by_folder: dict[Optional[int], list[ReportRow]] = {}
        for r in reports:
            reports_by_folder.setdefault(r.folder_id, []).append(r)
        for siblings in reports_by_folder.values():
            siblings.sort(key=lambda r: r.name.lower())

        for f in report_folders:
            folder_item = QTreeWidgetItem([f.name, ""])
            folder_item.setData(0, Qt.UserRole, f.id)
            folder_item.setData(0, KIND_ROLE, "report_folder")
            folder_item.setFlags(Qt.ItemIsEnabled)
            font = folder_item.font(0)
            font.setBold(True)
            folder_item.setFont(0, font)
            reports_header.addChild(folder_item)
            # Report folders show no balance — let the name span both
            # columns so its text doesn't get truncated by a ghost
            # balance column.
            folder_item.setFirstColumnSpanned(True)
            for r in reports_by_folder.get(f.id, []):
                report_item = self._make_report_item(r)
                folder_item.addChild(report_item)
                report_item.setFirstColumnSpanned(True)
            folder_item.setExpanded(True)

        for r in sorted(
            reports_by_folder.get(None, []),
            key=lambda r: r.name.lower(),
        ):
            report_item = self._make_report_item(r)
            reports_header.addChild(report_item)
            report_item.setFirstColumnSpanned(True)

        reports_header.setExpanded(True)

        # Default selection: the first selectable row (All transactions),
        # unless caller's reload() restored a previous one.
        self.select_all_transactions()

    def _make_section_header(
        self, title: str, kind: str, with_top_rule: bool,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem([title, ""])
        item.setData(0, KIND_ROLE, kind)
        # Non-selectable, but still enabled so children render correctly
        # and the row can be clicked (we ignore the click).
        item.setFlags(Qt.ItemIsEnabled)
        font = QFont()
        font.setBold(True)
        font.setCapitalization(QFont.AllUppercase)
        font.setPointSizeF(font.pointSizeF() * 0.85)
        item.setFont(0, font)
        item.setForeground(0, QBrush(_HEADER_FG))
        item.setBackground(0, QBrush(_HEADER_BG))
        item.setBackground(1, QBrush(_HEADER_BG))
        if with_top_rule:
            # Visual separator between sections — a slate-200 single-line
            # frame on the header itself. Implemented as the header's
            # foreground colour brushed onto a thicker top via a stylized
            # font-metric trick won't paint cleanly, so we use a leading
            # blank padding row with a top border via setSizeHint and a
            # small italic spacer. The cleanest pragmatic shape: bump the
            # header's row height so a visible gap appears above it.
            hint = item.sizeHint(0)
            if hint.isValid():
                hint.setHeight(hint.height() + 10)
                item.setSizeHint(0, hint)
        return item

    def _make_account_item(
        self, account: AccountSummary, balances: dict[int, Decimal],
        is_closed: bool = False,
    ) -> QTreeWidgetItem:
        bal = balances.get(account.id, Decimal("0.00"))
        item = QTreeWidgetItem(
            [account.name, self._format(bal, account.currency)]
        )
        item.setData(0, Qt.UserRole, account.iri)
        item.setData(0, KIND_ROLE, "account")
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        tooltip = (
            f"{account.iri}\nType: {account.type}\nCurrency: {account.currency}"
        )
        if is_closed:
            # Muted text + a marker role so the register context menu offers
            # Reopen (ADR-069); still selectable so its register is viewable.
            item.setData(0, CLOSED_ROLE, True)
            item.setForeground(0, QBrush(_CLOSED_FG))
            item.setForeground(1, QBrush(_CLOSED_FG))
            tooltip += "\n(closed)"
        item.setToolTip(0, tooltip)
        return item

    def _make_report_item(self, report: ReportRow) -> QTreeWidgetItem:
        item = QTreeWidgetItem([report.name, ""])
        item.setData(0, Qt.UserRole, int(report.id))
        item.setData(0, KIND_ROLE, "report")
        item.setToolTip(
            0,
            f"{report.iri}\nType: {report.type}",
        )
        return item

    def _resolve_display_currency(self) -> str:
        """The currency a mixed-currency folder rolls up into: the configured
        base currency, else the first currency in use, else GBP."""
        if self._repo is None:
            return "GBP"
        base = self._repo.get_setting("base_currency")
        if base and base.strip():
            return base.strip().upper()
        in_use = self._repo.list_distinct_currencies()
        return in_use[0] if in_use else "GBP"

    def _folder_balance_cell(
        self, members: list[AccountSummary], balances: dict[int, Decimal],
    ) -> tuple[str, Optional[str]]:
        """Render a folder's roll-up balance + an optional tooltip.

        A single-currency folder shows its native sum (unchanged). A
        *mixed*-currency folder converts every member into the display
        currency via the ADR-035 FX layer and shows the converted sum — a
        bare cross-currency sum (the old behaviour) silently added, say, USD
        to GBP at par, which is the same class of bug ADR-055 fixed in Net
        Worth. Per that policy we never par-add: a non-zero member with no
        rate to the display currency is *excluded* and noted in the tooltip
        (marked with a trailing ``*``), not folded in at 1:1.
        """
        currencies = {a.currency for a in members}
        if len(currencies) <= 1:
            folder_currency = next(iter(currencies)) if currencies else None
            folder_sum = sum(
                (balances.get(a.id, Decimal("0.00")) for a in members),
                start=Decimal("0.00"),
            )
            return self._format(folder_sum, folder_currency), None

        if self._repo is None:
            # No FX access (repo-less caller / test) — preserve the old
            # no-symbol bare sum rather than invent a wrong symbol.
            folder_sum = sum(
                (balances.get(a.id, Decimal("0.00")) for a in members),
                start=Decimal("0.00"),
            )
            return self._format(folder_sum, None), None

        display = self._display_currency
        today = date.today().isoformat()
        total = Decimal("0.00")
        excluded: list[AccountSummary] = []
        for a in members:
            bal = balances.get(a.id, Decimal("0.00"))
            conv, _fb = self._repo.convert_amount(
                bal, from_ccy=a.currency, to_ccy=display, on_date=today,
            )
            if conv is None:
                if bal != 0:
                    excluded.append(a)
                continue
            total += conv

        text = self._format(total, display)
        tooltip: Optional[str] = None
        if excluded:
            text += " *"
            lines = "\n".join(f"  {a.name} ({a.currency})" for a in excluded)
            tooltip = (
                f"{len(excluded)} account(s) excluded — no exchange rate to "
                f"{display}. Set one in Manage ▸ Currencies.\n{lines}"
            )
        return text, tooltip

    @staticmethod
    def _format(amount: Decimal, currency: Optional[str]) -> str:
        body = f"{abs(amount):,.2f}"
        symbol = _CURRENCY_SYMBOLS.get(currency) if currency else None
        if symbol is None:
            return f"-{body}" if amount < 0 else body
        return f"-{symbol}{body}" if amount < 0 else f"{symbol}{body}"

    # ── signals / event handling ──

    def _on_selection_changed(self) -> None:
        """itemSelectionChanged fires only when a selectable row is picked.
        Section headers and folders aren't selectable so they don't reach
        here; if the selection set is empty we keep quiet (a folder click
        that cleared selection leaves the previous emission as the
        last word)."""
        items = self.selectedItems()
        if not items:
            return
        item = items[0]
        kind = item.data(0, KIND_ROLE)
        payload = item.data(0, Qt.UserRole)
        if kind == "all":
            self.selection_changed.emit("all_transactions", None)
        elif kind == "account":
            self.selection_changed.emit("account", payload)
        elif kind == "report":
            self.selection_changed.emit("report", int(payload))

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """Click on a folder row toggles its expansion (account folders
        and report folders alike). Section headers also toggle so the
        user can collapse a whole section."""
        if item is None:
            return
        kind = item.data(0, KIND_ROLE)
        if kind in (
            "folder", "closed_group", "report_folder",
            "section_accounts", "section_reports",
        ):
            item.setExpanded(not item.isExpanded())

    # ── public helpers used by RegisterWindow ──

    def current_selection_tuple(self) -> tuple[Optional[str], object]:
        """Return ``(kind, payload)`` of the current selection, or
        ``(None, None)`` if nothing selectable is selected. Used by the
        register window to round-trip selection across a sidebar reload."""
        items = self.selectedItems()
        if not items:
            return (None, None)
        item = items[0]
        kind = item.data(0, KIND_ROLE)
        payload = item.data(0, Qt.UserRole)
        if kind == "all":
            return ("all_transactions", None)
        if kind == "account":
            return ("account", payload)
        if kind == "report":
            return ("report", int(payload))
        return (None, None)

    def current_account_iri(self) -> Optional[str]:
        """Convenience for callers that only care about the account-iri
        case (back-compat with the ADR-015 single-arg API)."""
        kind, payload = self.current_selection_tuple()
        if kind == "account" and isinstance(payload, str):
            return payload
        return None

    def select_account_by_iri(self, iri: str) -> bool:
        """Find the account anywhere in the tree (root or inside a folder
        within the Accounts section) and make it current. Returns True
        if found."""
        for top_index in range(self.topLevelItemCount()):
            top = self.topLevelItem(top_index)
            if top.data(0, KIND_ROLE) != "section_accounts":
                continue
            if self._select_account_under(top, iri):
                return True
        return False

    def _select_account_under(
        self, parent: QTreeWidgetItem, iri: str,
    ) -> bool:
        for j in range(parent.childCount()):
            child = parent.child(j)
            kind = child.data(0, KIND_ROLE)
            if kind == "account" and child.data(0, Qt.UserRole) == iri:
                self.setCurrentItem(child)
                return True
            # Account leaves also live one level down inside folders and the
            # 'Closed accounts' group (ADR-069).
            if kind in ("folder", "closed_group"):
                for k in range(child.childCount()):
                    grand = child.child(k)
                    if (
                        grand.data(0, KIND_ROLE) == "account"
                        and grand.data(0, Qt.UserRole) == iri
                    ):
                        # Expand the closed group so the selection is visible.
                        if kind == "closed_group":
                            child.setExpanded(True)
                        self.setCurrentItem(grand)
                        return True
        return False

    def select_report_by_id(self, report_id: int) -> bool:
        """Find a report (root or inside a report folder) and make it
        current. Returns True if found."""
        for top_index in range(self.topLevelItemCount()):
            top = self.topLevelItem(top_index)
            if top.data(0, KIND_ROLE) != "section_reports":
                continue
            for j in range(top.childCount()):
                child = top.child(j)
                kind = child.data(0, KIND_ROLE)
                if kind == "report" and int(child.data(0, Qt.UserRole)) == report_id:
                    self.setCurrentItem(child)
                    return True
                if kind == "report_folder":
                    for k in range(child.childCount()):
                        grand = child.child(k)
                        if (
                            grand.data(0, KIND_ROLE) == "report"
                            and int(grand.data(0, Qt.UserRole)) == report_id
                        ):
                            self.setCurrentItem(grand)
                            return True
        return False

    def section_at_y(self, y: int) -> Optional[str]:
        """Return the section the viewport y-coordinate falls into:
        ``"accounts"``, ``"reports"``, or ``None`` if above the first
        section header.

        Used by the context-menu handler so a right-click in a section's
        empty space (below the last item, or in an empty Reports section)
        offers that section's verbs rather than always defaulting to the
        Accounts menu. Falls through `visualItemRect` which respects
        scrolling and expansion state."""
        accounts_top: Optional[int] = None
        reports_top: Optional[int] = None
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            kind = item.data(0, KIND_ROLE)
            rect = self.visualItemRect(item)
            if not rect.isValid():
                continue
            if kind == "section_accounts":
                accounts_top = rect.top()
            elif kind == "section_reports":
                reports_top = rect.top()
        # Reports header is below Accounts (always); any y at/below the
        # reports header belongs to the reports section.
        if reports_top is not None and y >= reports_top:
            return "reports"
        if accounts_top is not None and y >= accounts_top:
            return "accounts"
        return None

    def select_all_transactions(self) -> None:
        """Make the 'All transactions' row current. Walks the Accounts
        section to find it (it's structurally the first child of that
        section header)."""
        for top_index in range(self.topLevelItemCount()):
            top = self.topLevelItem(top_index)
            if top.data(0, KIND_ROLE) != "section_accounts":
                continue
            for j in range(top.childCount()):
                child = top.child(j)
                if child.data(0, KIND_ROLE) == "all":
                    self.setCurrentItem(child)
                    return

    def _restore_selection(
        self, kind: Optional[str], payload: object,
    ) -> bool:
        if kind == "all_transactions":
            self.select_all_transactions()
            return True
        if kind == "account" and isinstance(payload, str):
            return self.select_account_by_iri(payload)
        if kind == "report" and isinstance(payload, int):
            return self.select_report_by_id(payload)
        return False


# Backwards-compatible alias for callers that still import the old name.
# Removed entirely once those callers are migrated.
AccountSidebar = Sidebar
