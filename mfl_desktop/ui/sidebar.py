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

import json
from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QHeaderView, QPushButton, QTreeWidget,
    QTreeWidgetItem, QWidget,
)

from mfl_desktop.ui import tokens
from mfl_desktop.ui.chart_helpers import currency_symbol
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

# Section header + closed-account colours come from the design tokens
# (ADR-076) so the sidebar follows the light/dark theme. Resolved at build
# time and re-applied by ``_restyle`` on a theme change.
# ADR-119: section headers read as MRL-style muted-caps *labels*, not a grey
# band — so the foreground is the lighter ``muted`` token and the background
# matches the panel ``surface`` (no fill). The uppercase + letter-spacing is
# applied to the font in ``_make_section_header``.
def _header_fg() -> QBrush:
    return QBrush(QColor(tokens.c("muted")))


def _header_bg() -> QBrush:
    return QBrush(QColor(tokens.c("surface")))


def _closed_fg() -> QBrush:
    return QBrush(QColor(tokens.c("subtle")))

# Currency glyphs come from chart_helpers.currency_symbol() — the one definition
# (ADR-159/165).


class Sidebar(QTreeWidget):
    """Two-column tree (Name | Balance), header hidden.

    Emits ``selection_changed(kind, payload)`` for the three selectable
    row kinds (see module docstring). Folder + section-header rows do
    not emit; they're display/grouping only.
    """

    selection_changed = Signal(str, object)
    # Emitted when the user flips the Today | Projected balance toggle
    # (ADR-131). The register window recomputes balances and reloads.
    balance_mode_changed = Signal(str)          # 'today' | 'projected'

    # QSettings key for the remembered balance mode (app-level, ADR-092).
    _BALANCE_MODE_KEY = "sidebar/balance_mode"

    # Per-file `setting` key for remembered group expansion (ADR-168). Unlike the
    # balance mode (an app-level QSettings preference), this lives *inside* the
    # .mfl's `setting` table (ADR-092): the folder ids it references are per-file,
    # so an app-level key would bleed one file's collapse state onto another.
    # Value is a JSON object mapping a stable group key → expanded bool, storing
    # only the groups the user has actually toggled so per-kind defaults still
    # apply.
    _GROUP_EXPANSION_KEY = "sidebar/group_expansion"

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
        # Today's balance (posted on/before today) vs projected (whole ledger,
        # incl. future-dated rows) — ADR-131. Remembered app-level; defaults to
        # 'today' so the sidebar shows the actual balance now.
        self._balance_mode = self.saved_balance_mode()
        # Remembered per-group expansion (ADR-168): read once from this file's
        # `setting` table (via ``self._repo``, set above) and kept in sync on
        # every user toggle. Read before ``_populate`` so the first build already
        # reflects the saved state.
        self._group_expansion = self._load_group_expansion()
        # ADR-119: objectName binds the flush, airier "navigation panel" QSS in
        # ui/theme.py (borderless, roomier rows) without affecting other trees.
        self.setObjectName("sidebar")
        self.setMinimumWidth(240)
        # No max width — the splitter handle is the constraint. The
        # earlier 540px cap left long account names ("Smile Current
        # Account") elided even when the user had dragged the splitter
        # wider, with the extra space appearing as dead whitespace
        # between the sidebar's right edge and the splitter handle.
        # macOS draws a blue focus ring around the focused item view; QSS
        # `outline: 0` doesn't remove it — this attribute does (ADR-076 fix).
        # Harmless on other platforms.
        self.setAttribute(Qt.WA_MacShowFocusRect, False)
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
        # Persist a group's expansion whenever it changes — this fires for every
        # route (row-body click, disclosure triangle, keyboard), unlike
        # ``itemClicked``. Connected *after* the initial ``_populate`` so building
        # the tree doesn't trip it, and ``reload`` blocks signals while it
        # rebuilds, so only genuine user toggles reach ``_remember_group_expansion``.
        self.itemExpanded.connect(self._remember_group_expansion)
        self.itemCollapsed.connect(self._remember_group_expansion)
        # ADR-076: re-apply header/closed colours when the theme switches
        # (these are set on items as brushes, not via QSS, so the global
        # re-style doesn't reach them).
        tokens.notifier.changed.connect(self._restyle)

    # ── balance mode (Today | Projected, ADR-131) ───────────────────────────

    def balance_mode(self) -> str:
        """'today' (posted on/before today) or 'projected' (whole ledger)."""
        return self._balance_mode

    @staticmethod
    def saved_balance_mode() -> str:
        """The remembered balance mode from QSettings ('today' default) — usable
        before a Sidebar instance exists (ADR-131)."""
        m = str(QSettings().value(Sidebar._BALANCE_MODE_KEY, "today") or "today")
        return m if m in ("today", "projected") else "today"

    def _make_balance_toggle(self) -> QWidget:
        """A compact segmented 'Today | Projected' pill (matches the ADR-113
        toggle style) placed on the Accounts header row. Reflects the current
        mode and re-emits ``balance_mode_changed`` on a change."""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addStretch(1)
        group = QButtonGroup(holder)
        group.setExclusive(True)
        for mode, label in (("today", "Today"), ("projected", "Projected")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(self._balance_mode == mode)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(
                "Balance as of today (excludes future-dated transactions)."
                if mode == "today" else
                "Projected balance including future-dated transactions."
            )
            tokens.themed(
                b,
                "QPushButton { padding: 1px 6px; border: 1px solid {border}; "
                "background-color: {surface}; color: {muted}; font-size: 9px; }"
                "QPushButton:checked { background-color: {accent}; "
                "color: {surface}; border-color: {accent}; font-weight: bold; }"
                "QPushButton:hover:!checked { background-color: {surface_alt}; }",
            )
            group.addButton(b)
            b.clicked.connect(lambda _c, m=mode: self._on_balance_mode_clicked(m))
            row.addWidget(b)
        return holder

    def _on_balance_mode_clicked(self, mode: str) -> None:
        if mode == self._balance_mode:
            return
        self._balance_mode = mode
        QSettings().setValue(self._BALANCE_MODE_KEY, mode)
        self.balance_mode_changed.emit(mode)

    def set_repo(self, repo: Optional[Repository]) -> None:
        """Repoint the sidebar at a newly-adopted file's repo (ADR-168 file
        switch). The register window drives the display data through ``reload``,
        but the repo-derived state the sidebar owns — the mixed-currency display
        currency and the remembered per-group expansion — must be refreshed here,
        or a file switch keeps reading the previous file's settings. Mirrors the
        Home view's ``set_repo``. Call *before* ``reload`` so the rebuild uses the
        new file's saved expansion."""
        self._repo = repo
        self._display_currency = self._resolve_display_currency()
        self._group_expansion = self._load_group_expansion()

    # ── group expansion memory (per-file, ADR-168) ──────────────────────────

    @staticmethod
    def _group_key(item: QTreeWidgetItem) -> Optional[str]:
        """A stable persistence key for a collapsible group row, or ``None`` for
        rows whose expansion we don't remember (leaf/selectable rows).

        Folder rows carry a DB-backed id (``Qt.UserRole``) that is stable across
        restarts; account- and report-folder ids share one integer space, so the
        kind prefix keeps them from colliding. The singleton rows (the two
        section headers and the closed-accounts group) key off their kind alone.
        """
        kind = item.data(0, KIND_ROLE)
        if kind in ("folder", "report_folder"):
            return f"{kind}:{item.data(0, Qt.UserRole)}"
        if kind in ("section_accounts", "section_reports", "closed_group"):
            return kind
        return None

    def _load_group_expansion(self) -> dict[str, bool]:
        """The remembered ``{group_key: expanded}`` map from this file's
        ``setting`` table. Empty when repo-less (tests) or nothing saved yet; a
        corrupt value degrades to empty rather than raising."""
        if self._repo is None:
            return {}
        raw = self._repo.get_setting(self._GROUP_EXPANSION_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): bool(v) for k, v in data.items()}

    def _expanded_for(self, item: QTreeWidgetItem, *, default: bool) -> bool:
        """Whether ``item`` should be expanded on build — the user's saved choice
        if they've toggled this group, else the caller's per-kind default."""
        key = self._group_key(item)
        if key is None:
            return default
        return self._group_expansion.get(key, default)

    def _remember_group_expansion(self, item: QTreeWidgetItem) -> None:
        """Record a group's new expansion after the user toggled it, writing the
        whole map back to this file's ``setting`` table. Best-effort: a failed
        write must never break navigation, so any error is swallowed."""
        if self._repo is None:
            return
        key = self._group_key(item)
        if key is None:
            return
        self._group_expansion[key] = item.isExpanded()
        try:
            self._repo.set_setting(
                self._GROUP_EXPANSION_KEY, json.dumps(self._group_expansion)
            )
        except Exception:
            pass

    def _restyle(self) -> None:
        """Re-apply the token-derived header/closed-row brushes to the live
        items after a theme change."""
        def walk(item) -> None:
            kind = item.data(0, KIND_ROLE)
            if kind in ("section_accounts", "section_reports"):
                item.setForeground(0, _header_fg())
                item.setBackground(0, _header_bg())
                item.setBackground(1, _header_bg())
            elif kind == "closed_group" or item.data(0, CLOSED_ROLE):
                item.setForeground(0, _closed_fg())
                item.setForeground(1, _closed_fg())
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))

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
        # ── Home (ADR-075) — a top-level landing row above the sections ──
        home_item = QTreeWidgetItem(["Home", ""])
        home_item.setData(0, KIND_ROLE, "home")
        home_item.setFirstColumnSpanned(True)
        hfont = home_item.font(0)
        hfont.setBold(True)
        home_item.setFont(0, hfont)
        self.addTopLevelItem(home_item)

        # ── Accounts section ──
        accounts_header = self._make_section_header(
            "ACCOUNTS", "section_accounts", with_top_rule=False,
        )
        self.addTopLevelItem(accounts_header)
        # The Today | Projected balance toggle (ADR-131) sits in column 1 of the
        # Accounts header, so the header is NOT column-spanned here.
        toggle = self._make_balance_toggle()
        self.setItemWidget(accounts_header, 1, toggle)
        # Guarantee column 1 is wide enough for the toggle even when every
        # balance is short (ResizeToContents ignores cell widgets, so a narrow
        # ledger would otherwise clip "Projected"). Bounds the min for all
        # sections — harmless for the stretchy name column.
        toggle.adjustSize()
        # +16 covers the tree cell's internal padding, which otherwise squeezes
        # the widget below its sizeHint and clips "Projected".
        self.header().setMinimumSectionSize(
            max(self.header().minimumSectionSize(), toggle.sizeHint().width() + 16)
        )

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
            folder_item.setExpanded(self._expanded_for(folder_item, default=True))

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
            closed_group.setForeground(0, _closed_fg())
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
            closed_group.setExpanded(self._expanded_for(closed_group, default=False))

        accounts_header.setExpanded(self._expanded_for(accounts_header, default=True))

        # ── Reports section ──
        # Only when there is something to put under it (ADR-165). A brand-new
        # file has no saved reports, and the header was drawn anyway — leaving a
        # bare "REPORTS" caption dangling at the bottom of the sidebar with
        # nothing beneath it, which reads as a section that failed to load. A
        # folder with no reports in it still counts: the folder is content.
        # (The header doubles as the drag-drop zone anchor — see `_zone_at` —
        # but with no reports and no folders there is nothing to drop.)
        if not reports and not report_folders:
            self.select_all_transactions()
            return

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
            folder_item.setExpanded(self._expanded_for(folder_item, default=True))

        for r in sorted(
            reports_by_folder.get(None, []),
            key=lambda r: r.name.lower(),
        ):
            report_item = self._make_report_item(r)
            reports_header.addChild(report_item)
            report_item.setFirstColumnSpanned(True)

        reports_header.setExpanded(self._expanded_for(reports_header, default=True))

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
        # ADR-119: a little tracking makes the muted-caps label read as a
        # section heading rather than shouted text.
        font.setLetterSpacing(QFont.PercentageSpacing, 108)
        item.setFont(0, font)
        item.setForeground(0, _header_fg())
        item.setBackground(0, _header_bg())
        item.setBackground(1, _header_bg())
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
            item.setForeground(0, _closed_fg())
            item.setForeground(1, _closed_fg())
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
        """A balance with its currency marked (ADR-165).

        The old local table returned *no symbol at all* for a currency it didn't
        know, so a CHF or CAD account rendered as a bare "1,234.00" —
        indistinguishable from sterling in a column that also holds sterling.
        ``currency_symbol`` falls back to the code ("CHF 1,234.00"), so a balance
        is never unlabelled. A row with no currency (a mixed-currency folder
        total) still gets no symbol, because there isn't one to give.
        """
        body = f"{abs(amount):,.2f}"
        symbol = currency_symbol(currency) if currency else ""
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
        if kind == "home":
            self.selection_changed.emit("home", None)
        elif kind == "all":
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

    def select_home(self) -> None:
        """Make the top-level Home row current (ADR-075)."""
        for top_index in range(self.topLevelItemCount()):
            top = self.topLevelItem(top_index)
            if top.data(0, KIND_ROLE) == "home":
                self.setCurrentItem(top)
                return

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
