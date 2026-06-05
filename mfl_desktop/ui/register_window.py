"""Register main window — top-level Qt surface for the desktop app.

Multi-account: a left-hand sidebar lists accounts with "All transactions"
on top. Selecting an account swaps the model and column layout; selecting
"All transactions" shows the cross-account aggregate (Account column added,
Balance column hidden — see project-all-transactions-view in memory).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.import_engine.import_service import ImportService
from mfl_desktop.ui.delegates import CategoryDelegate, StatusDelegate
from mfl_desktop.ui.filter_proxy import TransactionFilterProxy
from mfl_desktop.ui.register_model import TransactionTableModel
from mfl_desktop.ui.sidebar import AccountSidebar

STATUSES = ("Pending", "Uncleared", "Cleared", "Reconciled")

# Per-column default widths, keyed by attribute name so they apply to whichever
# mode the model is in.
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


class RegisterWindow(QMainWindow):
    def __init__(
        self,
        repo: Repository,
        initial_account_iri: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._repo = repo
        self._service = ImportService(repo)
        self._categories = repo.list_categories_flat()
        self._account: Optional[AccountSummary] = None  # None == all-transactions mode

        self.resize(1360, 760)

        # ── sidebar + filter bar ──

        accounts = repo.list_accounts()
        self._sidebar = AccountSidebar(accounts)
        self._sidebar.selection_changed.connect(self._on_sidebar_change)

        search = QLineEdit()
        search.setPlaceholderText("Search payee or memo…")
        search.textChanged.connect(lambda s: self._proxy.set_search(s))

        status_combo = QComboBox()
        status_combo.addItems(["All", *STATUSES])
        status_combo.currentTextChanged.connect(lambda s: self._proxy.set_status(s))

        self._category_combo = QComboBox()
        self._populate_category_combo()
        self._category_combo.currentIndexChanged.connect(
            lambda i: self._proxy.set_category_id(self._category_combo.itemData(i))
        )

        filter_bar = QHBoxLayout()
        filter_bar.setContentsMargins(8, 8, 8, 4)
        filter_bar.addWidget(QLabel("Search:"))
        filter_bar.addWidget(search, stretch=2)
        filter_bar.addSpacing(12)
        filter_bar.addWidget(QLabel("Status:"))
        filter_bar.addWidget(status_combo)
        filter_bar.addSpacing(12)
        filter_bar.addWidget(QLabel("Category:"))
        filter_bar.addWidget(self._category_combo, stretch=1)

        # ── table ──

        self._table = QTableView()
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setEditTriggers(
            QTableView.DoubleClicked
            | QTableView.SelectedClicked
            | QTableView.EditKeyPressed
        )
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(22)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.horizontalHeader().setStretchLastSection(False)

        # Proxy is created once; source model swaps on account change.
        self._model: TransactionTableModel = TransactionTableModel(repo, account_id=None)
        self._proxy = TransactionFilterProxy(self._model)
        self._table.setModel(self._proxy)

        # ── layout: splitter on left (sidebar), table area on right ──

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addLayout(filter_bar)
        right_layout.addWidget(self._table)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 1140])
        self.setCentralWidget(splitter)

        self.setStatusBar(QStatusBar(self))
        self._build_menus()

        self._proxy.layoutChanged.connect(self._update_status)
        self._proxy.modelReset.connect(self._update_status)
        self._proxy.rowsInserted.connect(self._update_status)
        self._proxy.rowsRemoved.connect(self._update_status)

        # ── initial selection ──

        if initial_account_iri is not None:
            self._select_account_in_sidebar(initial_account_iri)
        elif accounts:
            # Default to the first account, not the All Transactions row, since
            # for a single-account user that's the more useful starting view.
            self._select_account_in_sidebar(accounts[0].iri)
        else:
            self._show_all_transactions()

    # ── sidebar plumbing ──

    def _select_account_in_sidebar(self, account_iri: str) -> None:
        for i in range(self._sidebar.count()):
            if self._sidebar.item(i).data(Qt.UserRole) == account_iri:
                self._sidebar.setCurrentRow(i)
                return
        # Account not in sidebar (deleted?) — fall back to all-transactions
        self._show_all_transactions()

    def _on_sidebar_change(self, account_iri: Optional[str]) -> None:
        if account_iri is None:
            self._show_all_transactions()
        else:
            self._show_account(account_iri)

    # ── view modes ──

    def _show_account(self, account_iri: str) -> None:
        acct = self._repo.get_account_by_iri(account_iri)
        if acct is None:
            self._show_all_transactions()
            return
        self._account = acct
        self.setWindowTitle(
            f"My Financial Life — {acct.name}  ·  {acct.currency}"
        )
        self._set_model(TransactionTableModel(self._repo, account_id=acct.id))
        self._import_action.setEnabled(True)
        self._import_action.setToolTip("Import OFX / QFX / CSV into this account")

    def _show_all_transactions(self) -> None:
        self._account = None
        self.setWindowTitle("My Financial Life — All transactions")
        self._set_model(TransactionTableModel(self._repo, account_id=None))
        self._import_action.setEnabled(False)
        self._import_action.setToolTip(
            "Select an account in the sidebar to import into it"
        )

    def _set_model(self, model: TransactionTableModel) -> None:
        """Swap the source model and reattach delegates + column widths for
        the new column layout."""
        self._model = model
        self._proxy.setSourceModel(self._model)
        self._model.reload()

        col_index = {name: i for i, (_, name, _) in enumerate(self._model.COLUMNS)}
        # Clear all delegates then reattach where applicable, since column
        # positions differ between modes.
        for i in range(len(self._model.COLUMNS)):
            self._table.setItemDelegateForColumn(i, None)
        if "category_name" in col_index:
            self._table.setItemDelegateForColumn(
                col_index["category_name"],
                CategoryDelegate(self._categories, self._table),
            )
        if "status" in col_index:
            self._table.setItemDelegateForColumn(
                col_index["status"],
                StatusDelegate(self._table),
            )

        self._set_default_column_widths()
        if "posted_date" in col_index:
            self._table.sortByColumn(col_index["posted_date"], Qt.DescendingOrder)
        self._update_status()

    def _set_default_column_widths(self) -> None:
        for i, (_, name, _) in enumerate(self._model.COLUMNS):
            self._table.setColumnWidth(i, _COLUMN_WIDTHS.get(name, 120))

    def _update_status(self) -> None:
        visible = self._proxy.rowCount()
        total = self._model.rowCount()
        suffix = "" if self._account is not None else "  (across all accounts)"
        self.statusBar().showMessage(
            f"Showing {visible:,} of {total:,} transactions{suffix}"
        )

    # ── menus ──

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        self._import_action = QAction("&Import…", self)
        self._import_action.setShortcut(QKeySequence("Ctrl+O"))
        self._import_action.triggered.connect(self._on_import)
        file_menu.addAction(self._import_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    # ── import ──

    def _on_import(self) -> None:
        if self._account is None:
            QMessageBox.information(
                self, "Pick an account",
                "Select an account in the sidebar first — imports always "
                "target a specific account.",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import transactions", "",
            "Bank statements (*.ofx *.qfx *.csv);;All files (*)",
        )
        if not path:
            return
        try:
            file_bytes = Path(path).read_bytes()
            token, next_step = self._service.parse_and_stage(
                file_bytes, path, self._account.iri,
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Import failed",
                f"Could not parse {Path(path).name}:\n\n{e}",
            )
            return

        if next_step == "map":
            QMessageBox.information(
                self, "Column mapping needed",
                "This CSV format requires column mapping, which is coming "
                "in a future update. Please use OFX/QFX or a Banktivity "
                "export for now.",
            )
            return

        # Known format — commit directly with suggested status and auto-accept
        # all potential matches (per ADR-010 §6 and the no-dialog-for-known-
        # imports feedback). Nothing to ask the user; just do it.
        pending = self._service.get_pending(token)
        if pending is None:
            return
        accepted = {
            tx.fitid for tx in pending.transactions
            if tx.status == "potential_match"
        }
        try:
            result = self._service.commit_import(
                token, pending.suggested_status, accepted,
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Import failed",
                f"The import could not be committed:\n\n{e}",
            )
            return

        self._reload_after_import()
        self.statusBar().showMessage(
            f"Imported {result.imported} new into {self._account.name} · "
            f"{result.skipped} skipped · "
            f"{result.matched} matched  "
            f"(status: {pending.suggested_status})",
            10_000,
        )

    def _reload_after_import(self) -> None:
        """Reload transactions and refresh anything that depends on the
        category list (the import may have created new categories)."""
        self._model.reload()
        self._categories = self._repo.list_categories_flat()
        col_index = {name: i for i, (_, name, _) in enumerate(self._model.COLUMNS)}
        if "category_name" in col_index:
            self._table.setItemDelegateForColumn(
                col_index["category_name"],
                CategoryDelegate(self._categories, self._table),
            )
        self._populate_category_combo()

    def _populate_category_combo(self) -> None:
        """Rebuild the filter-bar Category combo. Preserves the
        currently-selected filter id where possible."""
        current_id = (
            self._category_combo.currentData()
            if self._category_combo.count() else None
        )
        self._category_combo.blockSignals(True)
        self._category_combo.clear()
        self._category_combo.addItem("All", userData=None)
        restore_index = 0
        for i, c in enumerate(self._categories, start=1):
            label = f"{c.name} ({c.parent_name})" if c.parent_name else c.name
            self._category_combo.addItem(label, userData=c.id)
            if c.id == current_id:
                restore_index = i
        self._category_combo.setCurrentIndex(restore_index)
        self._category_combo.blockSignals(False)
