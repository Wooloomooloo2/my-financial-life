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
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
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
from mfl_desktop.ui.filter_proxy import TransactionFilterProxy
from mfl_desktop.ui.register_model import TransactionTableModel


STATUSES = ("Pending", "Uncleared", "Cleared", "Reconciled")


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

    @classmethod
    def for_category(
        cls, *, account_id: int, account_name: str,
        category_id: int, category_label: str, period_key: str,
        custom_start: Optional[date] = None,
        custom_end: Optional[date] = None,
    ) -> "TxnListFilter":
        return cls(
            account_id=account_id, account_name=account_name,
            category_id=category_id, category_label=category_label,
            payee_id=None, payee_label="",
            period_key=period_key, title_label=category_label,
            custom_start=custom_start, custom_end=custom_end,
        )

    @classmethod
    def for_payee(
        cls, *, account_id: int, account_name: str,
        payee_id: int, payee_label: str, period_key: str,
        custom_start: Optional[date] = None,
        custom_end: Optional[date] = None,
    ) -> "TxnListFilter":
        return cls(
            account_id=account_id, account_name=account_name,
            category_id=None, category_label="",
            payee_id=payee_id, payee_label=payee_label,
            period_key=period_key, title_label=payee_label,
            custom_start=custom_start, custom_end=custom_end,
        )

    def signature(self) -> tuple:
        """Hashable key for the single-instance-per-filter registry
        on the summary window (ADR-034 §3 window policy). Custom bounds
        contribute to the signature so two distinct custom ranges open
        as two distinct windows."""
        return (
            self.account_id,
            self.period_key,
            self.category_id,
            self.payee_id,
            self.custom_start.isoformat() if self.custom_start else None,
            self.custom_end.isoformat() if self.custom_end else None,
        )


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
        self._category_descendant_ids: Optional[set[int]] = None
        self._date_from: Optional[str] = None
        self._date_to: Optional[str] = None

    def set_payee_id(self, payee_id: Optional[int]) -> None:
        self._payee_id = payee_id
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

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:
        if not super().filterAcceptsRow(source_row, parent):
            return False
        row = self.sourceModel().row_at(source_row)
        if self._payee_id is not None and row.payee_id != self._payee_id:
            return False
        if self._category_descendant_ids is not None:
            if row.category_id not in self._category_descendant_ids:
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
}


# Chip styling — slate-100 pill with a small × button. Matches the
# Banktivity-ish chip look.
_CHIP_STYLE = (
    "QFrame#filterChip { background-color: #e2e8f0; "
    "border: 1px solid #cbd5e1; border-radius: 12px; }"
    "QFrame#filterChip QLabel { background: transparent; border: none; "
    "color: #1e293b; font-size: 9pt; }"
    "QFrame#filterChip QPushButton { background: transparent; border: none; "
    "color: #475569; font-weight: bold; padding: 0 2px; }"
    "QFrame#filterChip QPushButton:hover { color: #0f172a; }"
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
        self._category_id: Optional[int] = txn_filter.category_id
        self._category_label: str = txn_filter.category_label
        self._payee_id: Optional[int] = txn_filter.payee_id
        self._payee_label: str = txn_filter.payee_label
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

        self._proxy: Optional[DrillDownFilterProxy] = None
        self._model: Optional[TransactionTableModel] = None
        self._set_model(self._account_id)

        # ── footer ──
        self._footer = QLabel("")
        self._footer.setStyleSheet("color: #475569; padding: 8px 4px;")

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
            btn.setStyleSheet(
                "QPushButton { padding: 5px 12px; border: 1px solid #cbd5e1; "
                "border-radius: 14px; background-color: #ffffff; "
                "color: #334155; font-size: 9pt; }"
                "QPushButton:checked { background-color: #2563eb; "
                "color: #ffffff; border-color: #2563eb; font-weight: bold; }"
                "QPushButton:hover:!checked { background-color: #f1f5f9; }"
            )
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
        chip.setStyleSheet(_CHIP_STYLE)
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
        self._model = TransactionTableModel(self._repo, account_id=account_id)
        self._proxy = DrillDownFilterProxy(self._model)
        self._table.setModel(self._proxy)
        self._model.reload()
        self._attach_delegates()
        self._apply_column_widths()

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
        # Payee — exact id.
        self._proxy.set_payee_id(self._payee_id)
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
        self.setWindowTitle(" — ".join(parts) if parts else "Transactions")

        # Account chip — only when an account is the focus.
        if self._account_id is not None and self._account_name:
            self._chips_layout.addWidget(
                self._make_chip(self._account_name, self._on_remove_account)
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
        # Payee chip.
        if self._payee_id is not None and self._payee_label:
            self._chips_layout.addWidget(
                self._make_chip(
                    f"Payee: {self._payee_label}",
                    self._on_remove_payee,
                )
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

    def _on_remove_account(self) -> None:
        """Widen to cross-account — rebuilds the model from
        ``list_all_transactions`` (different column layout, Account
        column appears, Balance disappears)."""
        self._account_name = ""
        self._set_model(None)
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
        self._payee_label = ""
        if not self._category_label:
            self._title_label = ""
        self._apply_filter()
        self._refresh_chips_and_title()
        self._refresh_footer()

    # ── refresh on activate (matches BudgetWindow / AccountSummaryWindow) ──

    def event(self, ev):
        if ev.type() == QEvent.WindowActivate and self._model is not None:
            # Cheap re-pull so edits made in other windows show up.
            self._model.reload()
            self._refresh_footer()
        return super().event(ev)
