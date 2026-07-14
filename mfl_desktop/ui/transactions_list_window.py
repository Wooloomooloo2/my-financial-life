"""Drill-down "Transactions" window (ADR-034).

A focused, register-like view of the transactions behind a single
roll-up — typically opened by clicking a row in the per-account
summary screen's Top Payees / Top Categories panels. Re-uses the
existing :class:`TransactionTableModel` + :class:`TransactionFilterProxy`,
extending the proxy with date / payee / category-descendant filters.

Window structure
================

- A **breadcrumb chip strip** at the top — one chip per active filter
  (Account / Period / Category / Payee). Each chip has an × that
  removes that dimension; removing every chip leaves a generic
  "All transactions, all time" view rather than closing the window.
- A **period selector** (same six presets as the summary screen) sits
  under the chips so the user can broaden or tighten the time window
  inside the drill-down without going back to the parent.
- A **QTableView** wired to the model with the same inline delegates
  the main register uses (Payee / Category typeahead, Status combo).
  Edits route through the Repository and the parent summary picks them
  up on its next ``WindowActivate`` refresh.
- A **footer line** showing the filtered row count and the signed sum
  of the filtered amounts — quick sanity check that the drill-down
  matches the row that was clicked.

Filter semantics
================

- **Category**: when present, filters to the clicked category AND ITS
  DESCENDANTS (matches how reports walk the tree per ADR-018 / ADR-030);
  clicking "Groceries" surfaces Coffees and Eating out beneath it.
- **Payee**: exact id match (round 1 of ADR-029 keeps display un-rolled;
  round 2 will canonicalise at import time and downstream rollups
  inherit the fix).
- **Period**: ``period_bounds`` from :mod:`mfl_desktop.account_summary`
  resolves the preset key to ``(start, end)`` ISO date strings.

Window lifecycle
================

Parented to the summary window so closing the summary closes all its
drill-downs. ADR-034 settled the window policy: one drill-down per
distinct filter signature (so a double-click doesn't pile up dupes,
but clicking Tesco *and then* Shell opens two windows side-by-side).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QEvent, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.account_summary import (
    PERIOD_KEYS,
    PERIOD_LABELS,
    period_bounds,
    period_display_label,
)
from mfl_desktop.db.repository import Repository
from mfl_desktop.ui.custom_period_dialog import CustomPeriodDialog
from mfl_desktop.ui.delegates import (
    CategoryTypeaheadDelegate,
    PayeeTypeaheadDelegate,
    StatusDelegate,
)
from mfl_desktop.ui.bulk_edit_dialog import BulkEditDialog
from mfl_desktop.ui.filter_proxy import TransactionFilterProxy
from mfl_desktop.ui.register_model import TransactionTableModel
from mfl_desktop.ui.split_transaction_dialog import SplitTransactionDialog
from mfl_desktop.ui.investment_transaction_dialog import (
    InvestmentTransactionDialog,
)
from mfl_desktop.import_engine.qif_actions import is_categorisable
from mfl_desktop.ui import tokens



@dataclass(frozen=True)
class TxnListFilter:
    """The drill-down's initial filter state. The window holds its own
    mutable copy of these so chip-× removals and period swaps don't
    mutate the caller's value.

    ``custom_start`` and ``custom_end`` are populated only when
    ``period_key == "custom"`` — they let the drill-down inherit the
    custom range the parent summary screen had active when the row was
    clicked, so the drill-down opens on the same window of time."""
    account_id: Optional[int]              # None = cross-account
    account_name: str                      # '' if cross-account
    category_id: Optional[int]             # None = no category filter
    category_label: str
    payee_id: Optional[int]                # None = no payee filter
    payee_label: str
    period_key: str
    title_label: str                       # primary breadcrumb — e.g. "Eating out"
    custom_start: Optional[date] = None
    custom_end: Optional[date] = None
    # Rolled-up payee filter (ADR-066): when non-empty, match any of these
    # payee ids — a canonical payee plus its aliases (ADR-029), so the
    # drill-down matches what the rolled-up report row counted.
    payee_ids: tuple[int, ...] = ()
    # When True, match only transactions with NO payee (the report's
    # "(No payee)" group). Takes precedence over payee_id / payee_ids.
    payee_is_null: bool = False
    # Cash-flow kind drill (ADR-083 — Income & Expense report): when set
    # ('income' | 'expense') the window scopes to that kind's categories +
    # matching sign + non-transfer, reconciling with the report's bars.
    kind: Optional[str] = None
    kind_label: str = ""
    # Security drill (ADR-083 — Investment Returns report): when set, match
    # only transactions on that security (its buys / sells / dividends).
    security_id: Optional[int] = None
    security_label: str = ""
    # Multi-account subset scope (ADR-147): when a report is scoped to a *set*
    # of accounts (e.g. a rental-property group that excludes a credit card),
    # the drill-down narrows to exactly those accounts instead of opening the
    # whole file. Empty == no subset (use ``account_id`` / cross-account). The
    # window loads all transactions and the proxy filters to this set, so the
    # cross-account Account column is shown. ``account_ids_label`` is the chip
    # caption (e.g. "3 accounts").
    account_ids: tuple[int, ...] = ()
    account_ids_label: str = ""

    @classmethod
    def for_category(
        cls, *, account_id: int, account_name: str,
        category_id: int, category_label: str, period_key: str,
        custom_start: Optional[date] = None,
        custom_end: Optional[date] = None,
        account_ids: tuple[int, ...] = (),
        account_ids_label: str = "",
    ) -> "TxnListFilter":
        return cls(
            account_id=account_id, account_name=account_name,
            category_id=category_id, category_label=category_label,
            payee_id=None, payee_label="",
            period_key=period_key, title_label=category_label,
            custom_start=custom_start, custom_end=custom_end,
            account_ids=account_ids, account_ids_label=account_ids_label,
        )

    @classmethod
    def for_payee(
        cls, *, account_id: int, account_name: str,
        payee_id: int, payee_label: str, period_key: str,
        custom_start: Optional[date] = None,
        custom_end: Optional[date] = None,
        account_ids: tuple[int, ...] = (),
        account_ids_label: str = "",
    ) -> "TxnListFilter":
        return cls(
            account_id=account_id, account_name=account_name,
            category_id=None, category_label="",
            payee_id=payee_id, payee_label=payee_label,
            period_key=period_key, title_label=payee_label,
            custom_start=custom_start, custom_end=custom_end,
            account_ids=account_ids, account_ids_label=account_ids_label,
        )

    @classmethod
    def for_payees(
        cls, *, account_id: Optional[int], account_name: str,
        payee_ids: tuple[int, ...], payee_label: str, period_key: str,
        payee_is_null: bool = False,
        custom_start: Optional[date] = None,
        custom_end: Optional[date] = None,
        account_ids: tuple[int, ...] = (),
        account_ids_label: str = "",
    ) -> "TxnListFilter":
        """Drill-down for a rolled-up payee (ADR-066): match any id in
        ``payee_ids`` (canonical + aliases), or — when ``payee_is_null`` —
        transactions with no payee at all."""
        return cls(
            account_id=account_id, account_name=account_name,
            category_id=None, category_label="",
            payee_id=payee_ids[0] if payee_ids else None,
            payee_label=payee_label,
            period_key=period_key, title_label=payee_label,
            custom_start=custom_start, custom_end=custom_end,
            payee_ids=payee_ids, payee_is_null=payee_is_null,
            account_ids=account_ids, account_ids_label=account_ids_label,
        )

    @classmethod
    def for_kind(
        cls, *, account_id: Optional[int], account_name: str,
        kind: str, kind_label: str, period_key: str,
        custom_start: Optional[date] = None,
        custom_end: Optional[date] = None,
        account_ids: tuple[int, ...] = (),
        account_ids_label: str = "",
    ) -> "TxnListFilter":
        """Drill-down for an Income & Expense bar (ADR-083): every
        ``kind``-category flow (income inflows / expense outflows, transfers
        excluded) over the period — matches the report's kind-based totals."""
        return cls(
            account_id=account_id, account_name=account_name,
            category_id=None, category_label="",
            payee_id=None, payee_label="",
            period_key=period_key, title_label=kind_label,
            custom_start=custom_start, custom_end=custom_end,
            kind=kind, kind_label=kind_label,
            account_ids=account_ids, account_ids_label=account_ids_label,
        )

    @classmethod
    def for_security(
        cls, *, account_id: Optional[int], account_name: str,
        security_id: int, security_label: str, period_key: str,
        custom_start: Optional[date] = None,
        custom_end: Optional[date] = None,
        account_ids: tuple[int, ...] = (),
        account_ids_label: str = "",
    ) -> "TxnListFilter":
        """Drill-down for an Investment Returns security row (ADR-083): that
        security's buys / sells / dividends over the period."""
        return cls(
            account_id=account_id, account_name=account_name,
            category_id=None, category_label="",
            payee_id=None, payee_label="",
            period_key=period_key, title_label=security_label,
            custom_start=custom_start, custom_end=custom_end,
            security_id=security_id, security_label=security_label,
            account_ids=account_ids, account_ids_label=account_ids_label,
        )

    def signature(self) -> tuple:
        """Hashable key for the single-instance-per-filter registry
        on the summary window (ADR-034 §3 window policy). Custom bounds
        contribute to the signature so two distinct custom ranges open
        as two distinct windows."""
        return (
            self.account_id,
            self.account_ids,
            self.period_key,
            self.category_id,
            self.payee_id,
            self.payee_ids,
            self.payee_is_null,
            self.kind,
            self.security_id,
            self.custom_start.isoformat() if self.custom_start else None,
            self.custom_end.isoformat() if self.custom_end else None,
        )


def drilldown_account_scope(
    account_ids, name_for,
) -> tuple[Optional[int], str, tuple[int, ...], str]:
    """Resolve a report's account selection into drill-down scope args
    (ADR-147). Returns ``(account_id, account_name, account_ids,
    account_ids_label)`` to spread into a ``TxnListFilter`` factory:

    - **one** account → per-account drill (its id + name; no subset);
    - **several** → a subset (no single id; the id tuple + an "N accounts"
      label) so the drill-down narrows to exactly those accounts instead of
      leaking every account's rows;
    - **none** → cross-account (all empty), the whole file.

    ``name_for`` maps an account id to its display name (each caller wires its
    own account lookup)."""
    ids = list(account_ids)
    if len(ids) == 1:
        return ids[0], name_for(ids[0]), (), ""
    if len(ids) > 1:
        return None, "", tuple(ids), f"{len(ids)} accounts"
    return None, "", (), ""


class DrillDownFilterProxy(TransactionFilterProxy):
    """Extends the register's filter proxy with three extra dimensions
    used by the drill-down (ADR-034 §3):

    - ``set_date_range`` — period bounds resolved from the preset key.
    - ``set_payee_id`` — exact id match.
    - ``set_category_descendant_ids`` — replaces the base's single-id
      category filter; the caller passes the descendant set returned
      by :py:meth:`Repository.category_descendants` (which already
      includes the seed id itself).

    The base proxy's status / search / single-category filters still
    work — the drill-down leaves the single-category filter unset and
    uses the descendant-set instead. Status is not exposed in the v1
    drill-down UI but the base capability remains for future use.
    """

    def __init__(self, source: TransactionTableModel) -> None:
        super().__init__(source)
        self._payee_id: Optional[int] = None
        self._payee_ids: Optional[frozenset[int]] = None
        self._payee_is_null: bool = False
        self._category_descendant_ids: Optional[set[int]] = None
        self._date_from: Optional[str] = None
        self._date_to: Optional[str] = None
        self._kind: Optional[str] = None
        self._kind_cat_ids: Optional[frozenset[int]] = None
        self._security_id: Optional[int] = None
        self._account_ids: Optional[frozenset[int]] = None

    def set_payee_id(self, payee_id: Optional[int]) -> None:
        self._payee_id = payee_id
        self.invalidateRowsFilter()

    def set_payee_ids(self, payee_ids: Optional[set[int]]) -> None:
        """Match any payee in the set (canonical + aliases, ADR-066).
        ``None`` / empty clears the set filter."""
        self._payee_ids = frozenset(payee_ids) if payee_ids else None
        self.invalidateRowsFilter()

    def set_payee_null(self, is_null: bool) -> None:
        """When True, match only transactions with no payee."""
        self._payee_is_null = is_null
        self.invalidateRowsFilter()

    def set_category_descendant_ids(self, ids: Optional[set[int]]) -> None:
        self._category_descendant_ids = ids
        self.invalidateRowsFilter()

    def set_date_range(
        self, date_from: Optional[str], date_to: Optional[str],
    ) -> None:
        self._date_from = date_from
        self._date_to = date_to
        self.invalidateRowsFilter()

    def set_kind_filter(
        self, kind: Optional[str], category_ids: Optional[set[int]],
    ) -> None:
        """Income & Expense drill (ADR-083): ``kind`` is 'income' or
        'expense'; ``category_ids`` is that kind's category id set. A row
        passes only when it's on one of those categories, is non-transfer,
        and its sign matches the kind (income inflows / expense outflows) —
        the same definition as ``Repository.income_expense_series``."""
        self._kind = kind
        self._kind_cat_ids = frozenset(category_ids) if category_ids else None
        self.invalidateRowsFilter()

    def set_security_id(self, security_id: Optional[int]) -> None:
        """Investment Returns drill (ADR-083): match only this security."""
        self._security_id = security_id
        self.invalidateRowsFilter()

    def set_account_ids(self, account_ids: Optional[set[int]]) -> None:
        """Multi-account subset scope (ADR-147): accept a row only when its
        account is in the set. The model is loaded cross-account
        (``account_id=None``) so every scoped account's rows are present;
        this narrows them to exactly the report's account selection so the
        drill-down reconciles with the report's account-filtered totals.
        ``None`` / empty clears the subset (cross-account)."""
        self._account_ids = frozenset(account_ids) if account_ids else None
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        if not super().filterAcceptsRow(source_row, parent):
            return False
        row = self.sourceModel().row_at(source_row)
        if self._payee_is_null:
            if row.payee_id is not None:
                return False
        elif self._payee_ids is not None:
            if row.payee_id not in self._payee_ids:
                return False
        elif self._payee_id is not None and row.payee_id != self._payee_id:
            return False
        if self._category_descendant_ids is not None:
            # Split-aware (ADR-051, mirrors the register's base proxy): a split
            # parent's own category_id is Uncategorised, so also accept the row
            # when any of its split lines is in the drilled category's subtree —
            # otherwise a category that exists only on split lines drills to an
            # empty list even though the report counted those lines.
            if (
                row.category_id not in self._category_descendant_ids
                and self._category_descendant_ids.isdisjoint(row.split_category_ids)
            ):
                return False
        if self._kind is not None:
            if row.transfer_id is not None:
                return False
            if (self._kind_cat_ids is not None
                    and row.category_id not in self._kind_cat_ids):
                return False
            if self._kind == "income" and row.amount <= 0:
                return False
            if self._kind == "expense" and row.amount >= 0:
                return False
        if self._security_id is not None and row.security_id != self._security_id:
            return False
        if self._account_ids is not None and row.account_id not in self._account_ids:
            return False
        if self._date_from and row.posted_date < self._date_from:
            return False
        if self._date_to and row.posted_date > self._date_to:
            return False
        return True


# Per-column default widths, keyed by attribute name so they apply to
# whichever mode the model is in. Mirrors `_COLUMN_WIDTHS` in
# register_window.py — duplicated rather than imported to avoid pulling
# the whole register module into the drill-down's import graph.
_COLUMN_WIDTHS = {
    "posted_date":     110,
    "account_name":    180,
    "payee_name":      220,
    "category_name":   200,
    "status":          110,
    "memo":            280,
    "amount":          110,
    "running_balance": 130,
    # Investment register (ADR-043) — used when an investment drill-down adopts
    # the security-aware column layout.
    "action":          90,
    "security_symbol": 80,
    "security_name":   260,
    "quantity":        100,
    "price":           100,
}


# Chip styling — a soft pill with a small × button (the Banktivity-ish look).
#
# ADR-167: this was five frozen light-theme hexes, so in dark mode the chips
# stayed a pale slate pill with near-black text — a light island floating on the
# dark canvas. It is a *template* now, resolved per theme by tokens.themed.
# Each token's light value equals the hex it replaces, so light is unchanged.
# (tokens._format is a regex over {token}, not str.format — literal QSS braces
# pass through untouched and must NOT be escaped.)
_CHIP_STYLE = (
    "QFrame#filterChip { background-color: {border}; "
    "border: 1px solid {border_strong}; border-radius: 12px; }"
    "QFrame#filterChip QLabel { background: transparent; border: none; "
    "color: {heading}; font-size: 12px; }"
    "QFrame#filterChip QPushButton { background: transparent; border: none; "
    "color: {muted_strong}; font-weight: bold; padding: 0 2px; }"
    "QFrame#filterChip QPushButton:hover { color: {text}; }"
)


class TransactionsListWindow(QMainWindow):
    """The drill-down. Construct with ``(repo, txn_filter, parent)``;
    the constructor builds the UI and applies the initial filter."""

    def __init__(
        self,
        repo: Repository,
        txn_filter: TxnListFilter,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        # Mutable view of the filter — chips and period selector mutate
        # these in place rather than replacing the dataclass each turn.
        self._account_id: Optional[int] = txn_filter.account_id
        self._account_name: str = txn_filter.account_name
        # Multi-account subset scope (ADR-147). A subset loads the model
        # cross-account (account_id stays None) and narrows via the proxy.
        self._account_ids: Optional[set[int]] = (
            set(txn_filter.account_ids) if txn_filter.account_ids else None
        )
        self._account_ids_label: str = txn_filter.account_ids_label
        self._category_id: Optional[int] = txn_filter.category_id
        self._category_label: str = txn_filter.category_label
        self._payee_id: Optional[int] = txn_filter.payee_id
        self._payee_ids: Optional[set[int]] = (
            set(txn_filter.payee_ids) if txn_filter.payee_ids else None
        )
        self._payee_is_null: bool = txn_filter.payee_is_null
        self._payee_label: str = txn_filter.payee_label
        self._kind: Optional[str] = txn_filter.kind
        self._kind_label: str = txn_filter.kind_label
        self._security_id: Optional[int] = txn_filter.security_id
        self._security_label: str = txn_filter.security_label
        self._period_key: str = txn_filter.period_key
        self._title_label: str = txn_filter.title_label
        self._custom_start: Optional[date] = txn_filter.custom_start
        self._custom_end: Optional[date] = txn_filter.custom_end
        # For restoring the previous button on Custom-dialog cancel.
        self._previous_period: str = txn_filter.period_key

        self.resize(1100, 700)

        # ── chip strip ──
        self._chips_row = QWidget()
        self._chips_layout = QHBoxLayout(self._chips_row)
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        self._chips_layout.setSpacing(6)

        # ── period selector ──
        self._period_buttons: dict[str, QPushButton] = {}
        period_row = self._build_period_selector()

        # ── table ──
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(False)
        # ADR-105: match the register's edit triggers so inline editing is as
        # responsive here, and offer the selection-based Bulk Edit verb via a
        # context menu + Ctrl+E.
        self._table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.SelectedClicked
            | QAbstractItemView.EditKeyPressed
        )
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)
        # ADR-147: dialog-edited rows (splits, investment transactions) can't be
        # edited inline, so route a double-click to their detail dialog — the
        # same affordance the register gives — so a drill-down (e.g. the Cash
        # Flow "Interest Exp" node, whose rows are splits) is editable, not just
        # viewable.
        self._table.doubleClicked.connect(self._on_table_double_clicked)
        self._bulk_edit_action = QAction("Bulk Edit Selected…", self)
        self._bulk_edit_action.setShortcut(QKeySequence("Ctrl+E"))
        self._bulk_edit_action.triggered.connect(self._on_bulk_edit)
        self.addAction(self._bulk_edit_action)

        self._proxy: Optional[DrillDownFilterProxy] = None
        self._model: Optional[TransactionTableModel] = None
        self._set_model(self._account_id)

        # ── footer ──
        self._footer = QLabel("")
        tokens.themed(self._footer, "color: {muted_strong}; padding: 8px 4px;")

        # ── layout ──
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(10)
        v.addWidget(self._chips_row)
        v.addWidget(period_row)
        v.addWidget(self._table, stretch=1)
        v.addWidget(self._footer)
        self.setCentralWidget(container)

        self._refresh_chips_and_title()
        self._apply_filter()
        self._refresh_footer()

    # ── builders ──

    def _build_period_selector(self) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        self._period_group = QButtonGroup(self)
        self._period_group.setExclusive(True)
        for key in PERIOD_KEYS:
            btn = QPushButton(PERIOD_LABELS[key])
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            tokens.themed(btn, "QPushButton { padding: 5px 12px; border: 1px solid {border_strong}; border-radius: 14px; background-color: {surface}; color: {heading}; font-size: 12px; }QPushButton:checked { background-color: {accent}; color: {surface}; border-color: {accent}; font-weight: bold; }QPushButton:hover:!checked { background-color: {surface_alt}; }")
            btn.clicked.connect(
                lambda _checked=False, k=key: self._on_period_selected(k)
            )
            h.addWidget(btn)
            self._period_buttons[key] = btn
            self._period_group.addButton(btn)
        h.addStretch(1)

        self._period_buttons[self._period_key].setChecked(True)
        return row

    def _make_chip(
        self, label_text: str, on_remove,
    ) -> QFrame:
        """Build a chip. If ``on_remove`` is None the chip is rendered
        without an × — used for the Period chip, which is always set
        (the user changes period via the button row, not by removing it).
        """
        chip = QFrame()
        chip.setObjectName("filterChip")
        tokens.themed(chip, _CHIP_STYLE)
        h = QHBoxLayout(chip)
        h.setContentsMargins(10, 4, 10 if on_remove is None else 6, 4)
        h.setSpacing(6)
        h.addWidget(QLabel(label_text))
        if on_remove is not None:
            btn = QPushButton("×")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedWidth(18)
            btn.clicked.connect(on_remove)
            h.addWidget(btn)
        return chip

    # ── model wiring ──

    def _set_model(self, account_id: Optional[int]) -> None:
        """(Re-)build the underlying model + proxy. Called on init and
        whenever the Account chip is removed (which widens the source
        from single-account to all-transactions)."""
        self._account_id = account_id
        self._model = TransactionTableModel(
            self._repo, account_id=account_id,
            invest=self._is_investment_drilldown(account_id),
        )
        # ADR-105: warn before an inline edit lands on a reconciled row, same
        # gate the main register installs (ADR-040).
        self._model.reconciled_edit_guard = self._confirm_reconciled_edit
        self._proxy = DrillDownFilterProxy(self._model)
        self._table.setModel(self._proxy)
        self._model.reload()
        self._attach_delegates()
        self._apply_column_widths()

    def _is_investment_drilldown(self, account_id: Optional[int]) -> bool:
        """Whether this drill-down should use the security-aware column layout
        (Action/Symbol/Security/Qty/Price) rather than the cash one
        (Payee/Category). True when the drill is into a single investment
        account, or is scoped to a specific security across accounts (a
        security drill is inherently an investment view) — see ADR-109 follow-up.
        Previously the drill-down always used the cash columns, so an investment
        report's drill-through looked wrong."""
        if account_id is not None:
            acct = self._repo.get_account_by_id(account_id)
            return acct is not None and acct.family == "investment"
        return self._security_id is not None

    def _attach_delegates(self) -> None:
        """Same delegates the main register uses — typeahead payee +
        category, combo status. Edits propagate through the model's
        setData path into the Repository."""
        assert self._model is not None
        col_index = {name: i for i, (_, name, _) in enumerate(self._model.COLUMNS)}
        for i in range(len(self._model.COLUMNS)):
            self._table.setItemDelegateForColumn(i, None)
        if "payee_name" in col_index:
            self._table.setItemDelegateForColumn(
                col_index["payee_name"],
                PayeeTypeaheadDelegate(self._repo, self._table),
            )
        if "category_name" in col_index:
            self._table.setItemDelegateForColumn(
                col_index["category_name"],
                CategoryTypeaheadDelegate(
                    self._repo,
                    on_create_category=self._on_create_category_inline,
                    parent=self._table,
                ),
            )
        if "status" in col_index:
            self._table.setItemDelegateForColumn(
                col_index["status"],
                StatusDelegate(self._table),
            )

    def _on_create_category_inline(self, name: str) -> Optional[int]:
        """Confirm-and-create a brand-new top-level expense category from
        the drill-down's category typeahead — mirrors the register's
        inline-create policy (ADR-022). The drill-down doesn't own a
        cached filter combo so there's no extra view to refresh; on
        success we just re-apply the proxy filter and footer in case
        the new id widens the active descendant set."""
        clean = (name or "").strip()
        if not clean:
            return None
        confirm = QMessageBox.question(
            self, "Create category?",
            f"No category named {clean!r} exists.\n\n"
            f"Create it as a new top-level expense category?",
        )
        if confirm != QMessageBox.Yes:
            return None
        try:
            new_id = self._repo.create_category(
                name=clean, parent_id=None, kind="expense", source="user",
            )
        except Exception as e:  # noqa: BLE001 — surface as a dialog
            QMessageBox.critical(
                self, "Could not create category",
                f"The category was not created:\n\n{e}",
            )
            return None
        self._apply_filter()
        self._refresh_footer()
        return new_id

    def _apply_column_widths(self) -> None:
        assert self._model is not None
        header = self._table.horizontalHeader()
        for i, (_, name, _) in enumerate(self._model.COLUMNS):
            width = _COLUMN_WIDTHS.get(name)
            if width is not None:
                self._table.setColumnWidth(i, width)
        # Stretch the memo column so trailing space goes there.
        col_index = {name: i for i, (_, name, _) in enumerate(self._model.COLUMNS)}
        if "memo" in col_index:
            header.setSectionResizeMode(col_index["memo"], QHeaderView.Stretch)

    # ── filter application ──

    def _resolve_period(self) -> tuple[date, date]:
        """Active period bounds — honours a custom range when set
        (ADR-033 amendment 2026-06-06)."""
        today = date.today()
        if self._period_key == "custom":
            if self._custom_start is not None and self._custom_end is not None:
                return self._custom_start, self._custom_end
            # Defensive — shouldn't happen because the Custom dialog
            # only commits on Accepted. Fall back to last quarter.
            return period_bounds("quarter", today)
        return period_bounds(self._period_key, today)

    def _period_chip_label(self) -> str:
        return period_display_label(
            self._period_key, self._custom_start, self._custom_end,
        )

    def _apply_filter(self) -> None:
        assert self._proxy is not None
        # Category — descendants set.
        if self._category_id is not None:
            ids = self._repo.category_descendants(self._category_id)
            self._proxy.set_category_descendant_ids(ids)
        else:
            self._proxy.set_category_descendant_ids(None)
        # Payee — null group, rolled-up id set, or single exact id.
        self._proxy.set_payee_null(self._payee_is_null)
        self._proxy.set_payee_ids(self._payee_ids)
        self._proxy.set_payee_id(self._payee_id)
        # Cash-flow kind — Income & Expense drill (ADR-083). Resolve the
        # kind's category id set here (the proxy stays repo-free).
        if self._kind is not None:
            kind_ids = {
                c.id for c in self._repo.list_categories_flat(kinds=(self._kind,))
            }
            self._proxy.set_kind_filter(self._kind, kind_ids)
        else:
            self._proxy.set_kind_filter(None, None)
        # Security — Investment Returns drill (ADR-083).
        self._proxy.set_security_id(self._security_id)
        # Account subset — multi-account report scope (ADR-147).
        self._proxy.set_account_ids(self._account_ids)
        # Date range — from period preset.
        start, end = self._resolve_period()
        self._proxy.set_date_range(start.isoformat(), end.isoformat())

    # ── chip rebuilding ──

    def _refresh_chips_and_title(self) -> None:
        """Clear and rebuild the chip strip. Window title follows the
        same breadcrumb."""
        # Clear existing chips.
        while self._chips_layout.count():
            item = self._chips_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        # Title parts in display order — what's NOT in here ends up
        # only on the chips (e.g. account when cross-account).
        parts: list[str] = []
        if self._title_label:
            parts.append(self._title_label)
        parts.append(self._period_chip_label())
        if self._account_name:
            parts.append(self._account_name)
        elif self._account_ids is not None:
            parts.append(self._account_subset_label())
        self.setWindowTitle(" — ".join(parts) if parts else "Transactions")

        # Account chip — only when an account is the focus.
        if self._account_id is not None and self._account_name:
            self._chips_layout.addWidget(
                self._make_chip(self._account_name, self._on_remove_account)
            )
        # Account-subset chip (ADR-147) — the report's multi-account scope.
        # Removing it widens to cross-account, mirroring the single-account
        # chip's ×.
        elif self._account_ids is not None:
            self._chips_layout.addWidget(
                self._make_chip(
                    self._account_subset_label(), self._on_remove_account_subset,
                )
            )
        # Period chip — always shown, non-removable (user changes the
        # period via the button row, not by removing the chip).
        self._chips_layout.addWidget(
            self._make_chip(self._period_chip_label(), None)
        )
        # Category chip.
        if self._category_id is not None and self._category_label:
            self._chips_layout.addWidget(
                self._make_chip(
                    f"Category: {self._category_label}",
                    self._on_remove_category,
                )
            )
        # Payee chip — shown for an exact id, a rolled-up id set, or the
        # no-payee group (ADR-066).
        payee_active = (
            self._payee_id is not None
            or self._payee_ids is not None
            or self._payee_is_null
        )
        if payee_active and self._payee_label:
            self._chips_layout.addWidget(
                self._make_chip(
                    f"Payee: {self._payee_label}",
                    self._on_remove_payee,
                )
            )
        # Cash-flow kind chip (ADR-083 Income & Expense drill) — non-removable
        # (the kind is the drill's defining dimension, like the period).
        if self._kind is not None and self._kind_label:
            self._chips_layout.addWidget(self._make_chip(self._kind_label, None))
        # Security chip (ADR-083 Investment Returns drill) — non-removable.
        if self._security_id is not None and self._security_label:
            self._chips_layout.addWidget(
                self._make_chip(f"Security: {self._security_label}", None)
            )
        self._chips_layout.addStretch(1)

    def _refresh_footer(self) -> None:
        assert self._proxy is not None and self._model is not None
        n = self._proxy.rowCount()
        total = Decimal("0.00")
        for proxy_row in range(n):
            source_row = self._proxy.mapToSource(
                self._proxy.index(proxy_row, 0),
            ).row()
            total += self._model.row_at(source_row).amount
        sign = "-" if total < 0 else ""
        amt = abs(total)
        self._footer.setText(
            f"{n} transaction{'s' if n != 1 else ''} · "
            f"Total: {sign}£{amt:,.2f}"
        )

    # ── handlers ──

    def _on_period_selected(self, key: str) -> None:
        if key not in PERIOD_KEYS:
            return
        if key == "custom":
            today = date.today()
            if self._custom_start is not None and self._custom_end is not None:
                seed_from, seed_to = self._custom_start, self._custom_end
            elif self._previous_period != "custom":
                seed_from, seed_to = period_bounds(self._previous_period, today)
            else:
                seed_from, seed_to = period_bounds("quarter", today)
            dialog = CustomPeriodDialog(
                initial_from=seed_from, initial_to=seed_to, parent=self,
            )
            if dialog.exec() != CustomPeriodDialog.Accepted:
                self._period_buttons[self._previous_period].setChecked(True)
                return
            self._custom_start, self._custom_end = dialog.values()
        self._previous_period = self._period_key
        self._period_key = key
        self._apply_filter()
        self._refresh_chips_and_title()
        self._refresh_footer()

    def _account_subset_label(self) -> str:
        """Chip / title caption for a multi-account subset scope (ADR-147).
        Prefers the caller-supplied label, else '{n} accounts'."""
        n = len(self._account_ids or ())
        return self._account_ids_label or f"{n} account{'s' if n != 1 else ''}"

    def _on_remove_account(self) -> None:
        """Widen to cross-account — rebuilds the model from
        ``list_all_transactions`` (different column layout, Account
        column appears, Balance disappears)."""
        self._account_name = ""
        self._set_model(None)
        self._apply_filter()
        self._refresh_chips_and_title()
        self._refresh_footer()

    def _on_remove_account_subset(self) -> None:
        """Drop the multi-account subset scope (ADR-147) — widen to every
        account. The model is already cross-account, so just clear the proxy
        filter and rebuild the chips."""
        self._account_ids = None
        self._apply_filter()
        self._refresh_chips_and_title()
        self._refresh_footer()

    def _on_remove_category(self) -> None:
        self._category_id = None
        self._category_label = ""
        if self._title_label == self._category_label or self._title_label:
            # Title fallback when the filtering identity drops away.
            self._title_label = self._payee_label or ""
        self._apply_filter()
        self._refresh_chips_and_title()
        self._refresh_footer()

    def _on_remove_payee(self) -> None:
        self._payee_id = None
        self._payee_ids = None
        self._payee_is_null = False
        self._payee_label = ""
        if not self._category_label:
            self._title_label = ""
        self._apply_filter()
        self._refresh_chips_and_title()
        self._refresh_footer()

    # ── bulk edit (ADR-105) ──

    def _selected_txn_ids(self) -> list[int]:
        """Source-row ids for the currently-selected proxy rows — one entry
        per row regardless of the clicked column."""
        assert self._proxy is not None and self._model is not None
        sel = self._table.selectionModel()
        if sel is None:
            return []
        ids: list[int] = []
        for proxy_idx in sel.selectedRows():
            src = self._proxy.mapToSource(proxy_idx)
            if src.isValid():
                ids.append(self._model.row_at(src.row()).id)
        return ids

    def _on_table_context_menu(self, pos) -> None:
        ids = self._selected_txn_ids()
        if len(ids) < 2:
            return
        menu = QMenu(self._table)
        act = menu.addAction(f"Bulk Edit {len(ids)} Transactions…")
        act.triggered.connect(self._on_bulk_edit)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_bulk_edit(self) -> None:
        """Apply payee / category / status / memo to ≥2 selected rows, reusing
        the register's BulkEditDialog. Transfers and splits stay in the
        register (ADR-105) — a transfer-kind category is refused here."""
        ids = self._selected_txn_ids()
        if len(ids) < 2:
            return
        dialog = BulkEditDialog(
            self._repo.list_categories_flat(),
            len(ids),
            payee_names=self._repo.list_payee_names(),
            parent=self,
        )
        if dialog.exec() != BulkEditDialog.Accepted:
            return
        changes = dialog.values()
        if not changes:
            return
        new_cat = changes.get("category_id")
        if new_cat is not None and self._repo.get_category_kind(new_cat) == "transfer":
            QMessageBox.information(
                self, "Use the register for transfers",
                "Setting a transfer category turns transactions into transfers, "
                "which isn't supported in this drill-down view.\n\n"
                "Open the account register to do that.",
            )
            return
        reconciled = [i for i in ids if self._repo.is_reconciled(i)]
        if reconciled and not self._confirm_reconciled_bulk(len(reconciled)):
            return
        try:
            self._repo.bulk_update_transactions(ids, **changes)
        except Exception as e:  # noqa: BLE001 — surface as a dialog
            QMessageBox.critical(
                self, "Bulk edit",
                f"The changes were not applied:\n\n{e}",
            )
            return
        self._model.reload()
        self._apply_filter()
        self._refresh_footer()

    def _confirm_reconciled_edit(self, _txn_id: int) -> bool:
        """Model gate for an inline edit on a reconciled row (ADR-040)."""
        resp = QMessageBox.question(
            self, "Reconciled transaction",
            "This transaction is reconciled to a statement.\n\n"
            "Changing it may put that statement out of balance. Change anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return resp == QMessageBox.Yes

    def _confirm_reconciled_bulk(self, count: int) -> bool:
        resp = QMessageBox.question(
            self, "Reconciled transactions",
            f"{count} of the selected transactions are reconciled to a "
            "statement. Changing them may put it out of balance. Change anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return resp == QMessageBox.Yes

    # ── double-click → detail dialog (ADR-147) ──

    def _on_table_double_clicked(self, proxy_index) -> None:
        """Route a double-click on a dialog-edited row to its detail dialog —
        the same affordance the register offers. Split rows (ADR-051) open the
        split dialog; investment rows (ADR-048) open the investment dialog.
        Plain cash rows stay inline-editable (Qt's own double-click edit
        trigger handles them; this is a no-op for them). The account is
        resolved from the row itself, so this works in the single-account,
        cross-account, and multi-account-subset views alike."""
        assert self._proxy is not None and self._model is not None
        if not proxy_index.isValid():
            return
        source_index = self._proxy.mapToSource(proxy_index)
        if not source_index.isValid():
            return
        row = self._model.row_at(source_index.row())
        if row.action is not None:
            # ADR-086: the Category cell is inline-editable for cash income /
            # expense actions — don't hijack its double-click to open the dialog.
            col_name = self._model.COLUMNS[source_index.column()][1]
            if col_name == "category_name" and is_categorisable(row.action):
                return
            self._open_investment_txn_dialog(row)
        elif row.split_count:
            self._open_split_txn_dialog(row)

    def _open_split_txn_dialog(self, seed) -> None:
        """Edit a split parent (ADR-051) in the split dialog, resolving the
        account from the row, then reload + re-filter on save."""
        account = self._repo.get_account_by_id(seed.account_id)
        if account is None or account.family == "investment":
            return  # splits aren't supported on investment accounts
        if (
            self._repo.is_reconciled(seed.id)
            and not self._confirm_reconciled_edit(seed.id)
        ):
            return
        dialog = SplitTransactionDialog(
            self._repo, account, self._repo.list_categories_flat(),
            seed=seed, parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        self._model.reload()
        self._apply_filter()
        self._refresh_footer()

    def _open_investment_txn_dialog(self, seed) -> None:
        """Edit an investment transaction (ADR-048) in its dialog, resolving
        the account from the row, then reload + re-filter on save."""
        account = self._repo.get_account_by_id(seed.account_id)
        if account is None:
            return
        if (
            self._repo.is_reconciled(seed.id)
            and not self._confirm_reconciled_edit(seed.id)
        ):
            return
        dialog = InvestmentTransactionDialog(
            self._repo, account, seed=seed, parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        self._model.reload()
        self._apply_filter()
        self._refresh_footer()

    # ── refresh on activate (matches BudgetWindow / AccountSummaryWindow) ──

    def event(self, ev):
        # Guard on is_open(): on app shutdown the shared repo may already be
        # closed while a queued WindowActivate fires here, and reloading a closed
        # connection crashes the quit (ADR-109 follow-up) — same guard the budget
        # / account-summary windows use.
        if (
            ev.type() == QEvent.WindowActivate
            and self._model is not None
            and self._repo.is_open()
        ):
            # Cheap re-pull so edits made in other windows show up.
            self._model.reload()
            self._refresh_footer()
        return super().event(ev)
