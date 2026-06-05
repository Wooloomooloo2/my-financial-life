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
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.import_engine.import_service import ImportService
from mfl_desktop.ui.account_dialog import AccountDialog
from mfl_desktop.ui.bulk_edit_dialog import BulkEditDialog
from mfl_desktop.ui.categories_dialog import CategoriesDialog
from mfl_desktop.ui.delegates import CategoryDelegate, StatusDelegate
from mfl_desktop.ui.filter_proxy import TransactionFilterProxy
from mfl_desktop.ui.payees_dialog import PayeesDialog
from mfl_desktop.ui.register_model import TransactionTableModel
from mfl_desktop.ui.sidebar import KIND_ROLE, AccountSidebar
from mfl_desktop.ui.transaction_dialog import NewTransactionDialog

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
        folders = repo.list_folders()
        balances = repo.compute_account_balances()
        self._sidebar = AccountSidebar(accounts, folders, balances)
        self._sidebar.selection_changed.connect(self._on_sidebar_change)
        self._sidebar.setContextMenuPolicy(Qt.CustomContextMenu)
        self._sidebar.customContextMenuRequested.connect(
            self._on_sidebar_context_menu
        )

        search = QLineEdit()
        search.setPlaceholderText("Search payee, memo, amount, or date…")
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
        self._table.setSelectionMode(QTableView.ExtendedSelection)
        self._table.setEditTriggers(
            QTableView.DoubleClicked
            | QTableView.SelectedClicked
            | QTableView.EditKeyPressed
        )
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(22)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)

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
        splitter.setSizes([320, 1040])
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
        if not self._sidebar.select_account_by_iri(account_iri):
            # Account not in sidebar (deleted?) — fall back to all-transactions.
            self._sidebar.select_all_transactions()
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
        self._update_window_title()
        self._set_model(TransactionTableModel(self._repo, account_id=acct.id))
        self._import_action.setEnabled(True)
        self._import_action.setToolTip("Import OFX / QFX / CSV into this account")
        self._set_account_action_state(account_selected=True)

    def _show_all_transactions(self) -> None:
        self._account = None
        self._update_window_title()
        self._set_model(TransactionTableModel(self._repo, account_id=None))
        self._import_action.setEnabled(False)
        self._import_action.setToolTip(
            "Select an account in the sidebar to import into it"
        )
        self._set_account_action_state(account_selected=False)

    def _update_window_title(self) -> None:
        filename = self._repo.db_path.name
        if self._account is None:
            suffix = "All transactions"
        else:
            suffix = f"{self._account.name}  ·  {self._account.currency}"
        self.setWindowTitle(f"My Financial Life — {filename} — {suffix}")

    def _set_account_action_state(self, account_selected: bool) -> None:
        """Enable Edit/Delete only when a specific account is being viewed."""
        if hasattr(self, "_edit_account_action"):
            self._edit_account_action.setEnabled(account_selected)
            self._delete_account_action.setEnabled(account_selected)

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

        open_action = QAction("&Open…", self)
        open_action.setShortcut(QKeySequence("Ctrl+O"))
        open_action.triggered.connect(self._on_open)
        file_menu.addAction(open_action)

        save_copy_action = QAction("&Save Copy As…", self)
        save_copy_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        save_copy_action.triggered.connect(self._on_save_copy_as)
        file_menu.addAction(save_copy_action)

        file_menu.addSeparator()

        self._import_action = QAction("&Import…", self)
        self._import_action.setShortcut(QKeySequence("Ctrl+I"))
        self._import_action.triggered.connect(self._on_import)
        file_menu.addAction(self._import_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        txn_menu = self.menuBar().addMenu("&Transaction")

        self._new_txn_action = QAction("&New Transaction…", self)
        self._new_txn_action.setShortcut(QKeySequence("Ctrl+N"))
        self._new_txn_action.triggered.connect(self._on_new_transaction)
        txn_menu.addAction(self._new_txn_action)

        self._delete_txn_action = QAction("&Delete Transaction", self)
        self._delete_txn_action.setShortcut(QKeySequence.Delete)
        self._delete_txn_action.triggered.connect(self._on_delete_transactions)
        txn_menu.addAction(self._delete_txn_action)

        txn_menu.addSeparator()

        self._bulk_edit_action = QAction("&Bulk Edit Selected…", self)
        self._bulk_edit_action.setShortcut(QKeySequence("Ctrl+E"))
        self._bulk_edit_action.triggered.connect(self._on_bulk_edit)
        txn_menu.addAction(self._bulk_edit_action)

        # Also expose the actions on the window so the shortcuts fire while
        # the table has focus.
        self.addAction(self._new_txn_action)
        self.addAction(self._delete_txn_action)
        self.addAction(self._bulk_edit_action)

        account_menu = self.menuBar().addMenu("&Account")

        self._new_account_action = QAction("&New Account…", self)
        self._new_account_action.triggered.connect(self._on_new_account)
        account_menu.addAction(self._new_account_action)

        self._edit_account_action = QAction("&Edit Account…", self)
        self._edit_account_action.triggered.connect(self._on_edit_account)
        account_menu.addAction(self._edit_account_action)

        self._delete_account_action = QAction("&Delete Account…", self)
        self._delete_account_action.triggered.connect(self._on_delete_account)
        account_menu.addAction(self._delete_account_action)

        # Edit/Delete are only meaningful when a specific account is selected;
        # state is kept in sync by _set_account_action_state on view changes.
        self._set_account_action_state(account_selected=self._account is not None)

        account_menu.addSeparator()

        self._new_folder_action = QAction("New &Folder…", self)
        self._new_folder_action.triggered.connect(self._on_new_folder)
        account_menu.addAction(self._new_folder_action)

        manage_menu = self.menuBar().addMenu("&Manage")

        self._manage_payees_action = QAction("&Payees…", self)
        self._manage_payees_action.triggered.connect(self._on_manage_payees)
        manage_menu.addAction(self._manage_payees_action)

        self._manage_categories_action = QAction("&Categories…", self)
        self._manage_categories_action.triggered.connect(self._on_manage_categories)
        manage_menu.addAction(self._manage_categories_action)

    # ── new / delete transaction ──

    def _on_new_transaction(self) -> None:
        accounts = self._repo.list_accounts()
        if not accounts:
            QMessageBox.information(
                self, "No accounts",
                "Create an account before adding transactions.",
            )
            return
        default_id = self._account.id if self._account is not None else None
        dialog = NewTransactionDialog(
            accounts=accounts,
            categories=self._categories,
            default_account_id=default_id,
            parent=self,
        )
        if dialog.exec() != NewTransactionDialog.Accepted:
            return
        values = dialog.values()
        if values is None:
            return

        try:
            payee_id = self._repo.get_or_create_payee(values.payee_name)
            self._repo.insert_transaction(
                account_id=values.account_id,
                posted_date=values.posted_date,
                amount=values.amount,
                payee_id=payee_id,
                category_id=values.category_id,
                status=values.status,
                memo=values.memo,
                import_hash=None,
                import_batch_id=None,
            )
            self._repo.commit()
        except Exception as e:
            self._repo.rollback()
            QMessageBox.critical(
                self, "Could not save transaction",
                f"The transaction was not saved:\n\n{e}",
            )
            return

        self._model.reload()
        self._refresh_sidebar_balances()
        self.statusBar().showMessage("Transaction added", 4000)

    def _on_delete_transactions(self) -> None:
        ids = self._selected_txn_ids()
        if not ids:
            return
        msg = (
            f"Delete {len(ids)} transactions?"
            if len(ids) > 1
            else "Delete this transaction?"
        )
        confirm = QMessageBox.question(
            self, "Confirm delete", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            removed = self._repo.delete_transactions(ids)
        except Exception as e:
            QMessageBox.critical(
                self, "Could not delete",
                f"Delete failed:\n\n{e}",
            )
            return
        self._model.reload()
        self._refresh_sidebar_balances()
        self.statusBar().showMessage(
            f"Deleted {removed} transaction{'s' if removed != 1 else ''}",
            4000,
        )

    def _selected_txn_ids(self) -> list[int]:
        """Source-row ids for the currently-selected proxy rows. Uses the
        selection model's selectedRows so we get one entry per row regardless
        of which column was clicked."""
        selection = self._table.selectionModel()
        if selection is None:
            return []
        ids: list[int] = []
        for proxy_idx in selection.selectedRows():
            source_idx = self._proxy.mapToSource(proxy_idx)
            if not source_idx.isValid():
                continue
            ids.append(self._model.row_at(source_idx.row()).id)
        return ids

    def _on_table_context_menu(self, pos) -> None:
        ids = self._selected_txn_ids()
        menu = QMenu(self._table)
        menu.addAction(self._new_txn_action)
        if len(ids) >= 2:
            bulk_act = menu.addAction(f"Bulk Edit {len(ids)} Transactions…")
            bulk_act.triggered.connect(self._on_bulk_edit)
        delete_act = menu.addAction(
            f"Delete {len(ids)} Transactions" if len(ids) > 1
            else "Delete Transaction"
        )
        delete_act.setEnabled(bool(ids))
        delete_act.triggered.connect(self._on_delete_transactions)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_bulk_edit(self) -> None:
        ids = self._selected_txn_ids()
        if len(ids) < 2:
            self.statusBar().showMessage(
                "Bulk edit needs at least 2 selected transactions.", 4000,
            )
            return
        dialog = BulkEditDialog(self._categories, len(ids), self)
        if dialog.exec() != BulkEditDialog.Accepted:
            return
        changes = dialog.values()
        if not changes:
            return
        try:
            self._repo.bulk_update_transactions(ids, **changes)
        except Exception as e:
            QMessageBox.critical(
                self, "Bulk edit failed",
                f"The change was not applied:\n\n{e}",
            )
            return
        self._model.reload()
        # If a category changed, balances and sidebar don't move but the
        # category column does; if payee changed it doesn't affect totals.
        # Skip the sidebar refresh — bulk edit doesn't move amounts.
        self.statusBar().showMessage(
            f"Updated {len(ids)} transactions", 4000,
        )

    # ── file: open / save copy as ──

    _DB_FILTER = (
        "My Financial Life databases (*.mfl *.db);;"
        "All files (*)"
    )

    def _on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open My Financial Life database",
            str(self._repo.db_path.parent),
            self._DB_FILTER,
        )
        if not path:
            return
        new_path = Path(path)
        if new_path.resolve() == self._repo.db_path.resolve():
            self.statusBar().showMessage(
                "That file is already open.", 4000,
            )
            return
        try:
            new_repo = Repository(new_path)
        except Exception as e:
            QMessageBox.critical(
                self, "Could not open file",
                f"The file at {new_path} could not be opened as a "
                f"My Financial Life database:\n\n{e}",
            )
            return
        self._swap_repository(new_repo)
        self.statusBar().showMessage(f"Opened {new_path.name}", 5000)

    def _on_save_copy_as(self) -> None:
        default_name = self._repo.db_path.stem + ".mfl"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save copy as",
            str(self._repo.db_path.parent / default_name),
            "My Financial Life databases (*.mfl);;"
            "SQLite databases (*.db);;"
            "All files (*)",
        )
        if not path:
            return
        new_path = Path(path)
        # If the user didn't type an extension, default to .mfl so the file
        # picker filter actually matches next time they Open it.
        if new_path.suffix == "":
            new_path = new_path.with_suffix(".mfl")
        try:
            self._repo.save_copy(new_path)
        except Exception as e:
            QMessageBox.critical(
                self, "Save failed",
                f"Could not save copy to {new_path}:\n\n{e}",
            )
            return
        self.statusBar().showMessage(
            f"Saved copy to {new_path.name}", 5000,
        )

    def _swap_repository(self, new_repo: Repository) -> None:
        """Replace the working repository with a freshly opened one.

        Rebuilds every UI surface that depends on the repo: sidebar (accounts,
        folders, balances), the cached category list, the filter-bar combo,
        and the register model. The old repo is closed *after* the new one
        is fully wired in so a partial-failure path can still recover.
        """
        old_repo = self._repo
        self._repo = new_repo
        self._service = ImportService(new_repo)
        self._categories = new_repo.list_categories_flat()
        self._account = None
        # _reload_sidebar pulls fresh data via self._repo (= new_repo) and
        # selects an item; the sidebar's selection_changed signal then drives
        # _show_account / _show_all_transactions which rebuild the model.
        self._reload_sidebar(select_iri=None)
        self._populate_category_combo()
        self._update_window_title()
        old_repo.close()

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

        self._refresh_categories_view()
        self._refresh_sidebar_balances()
        self.statusBar().showMessage(
            f"Imported {result.imported} new into {self._account.name} · "
            f"{result.skipped} skipped · "
            f"{result.matched} matched  "
            f"(status: {pending.suggested_status})",
            10_000,
        )

    def _refresh_categories_view(self) -> None:
        """Reload everything that depends on the current category list:
        register rows (a merge/delete may have re-pointed category_id), the
        cached choice list, the category delegate, and the filter-bar combo.
        Called after import and after the category-management dialog."""
        self._model.reload()
        self._categories = self._repo.list_categories_flat()
        col_index = {name: i for i, (_, name, _) in enumerate(self._model.COLUMNS)}
        if "category_name" in col_index:
            self._table.setItemDelegateForColumn(
                col_index["category_name"],
                CategoryDelegate(self._categories, self._table),
            )
        self._populate_category_combo()

    # ── account CRUD ──

    def _on_new_account(self) -> None:
        dialog = AccountDialog(existing=None, parent=self)
        if dialog.exec() != AccountDialog.Accepted:
            return
        values = dialog.values()
        if values is None or values.type_key is None:
            return
        try:
            acct = self._repo.create_account(
                name=values.name,
                type_key=values.type_key,
                currency=values.currency,
                opening_balance=values.opening_balance,
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Could not create account",
                f"The account was not created:\n\n{e}",
            )
            return
        self._reload_sidebar(select_iri=acct.iri)
        self.statusBar().showMessage(f"Created account {acct.name!r}", 4000)

    def _on_edit_account(self) -> None:
        if self._account is None:
            return
        existing = self._repo.get_account_by_id(self._account.id)
        if existing is None:
            return
        dialog = AccountDialog(existing=existing, parent=self)
        if dialog.exec() != AccountDialog.Accepted:
            return
        values = dialog.values()
        if values is None:
            return

        # Currency change on a populated account doesn't convert stored
        # amounts; warn before saving.
        if (values.currency != existing.currency
                and self._repo.account_has_transactions(existing.id)):
            confirm = QMessageBox.warning(
                self, "Currency change",
                f"This account has transactions stored as {existing.currency}. "
                f"Changing the currency to {values.currency} does not convert "
                f"the existing amounts — they will simply be displayed as "
                f"{values.currency} from now on.\n\nProceed?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return

        try:
            updated = self._repo.update_account(
                existing.id,
                name=values.name,
                currency=values.currency,
                opening_balance=values.opening_balance,
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Could not save account",
                f"The account was not saved:\n\n{e}",
            )
            return
        self._reload_sidebar(select_iri=updated.iri)
        self.statusBar().showMessage(f"Updated account {updated.name!r}", 4000)

    def _on_delete_account(self) -> None:
        if self._account is None:
            return
        acct = self._account
        txn_count = self._repo.count_account_transactions(acct.id)
        if txn_count > 0:
            body = (
                f"Delete account {acct.name!r}?\n\n"
                f"This will also permanently delete {txn_count:,} "
                f"transaction{'s' if txn_count != 1 else ''} and any "
                f"associated import history. This cannot be undone."
            )
        else:
            body = (
                f"Delete account {acct.name!r}?\n\nThis cannot be undone."
            )
        confirm = QMessageBox.warning(
            self, "Confirm delete", body,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            removed = self._repo.delete_account(acct.id)
        except Exception as e:
            QMessageBox.critical(
                self, "Could not delete account",
                f"The account was not deleted:\n\n{e}",
            )
            return
        self._reload_sidebar(select_iri=None)
        self.statusBar().showMessage(
            f"Deleted account {acct.name!r} "
            f"({removed:,} transaction{'s' if removed != 1 else ''} cascaded)",
            6000,
        )

    # ── folder ops (ADR-015) ──

    def _on_new_folder(self) -> None:
        name, ok = QInputDialog.getText(
            self, "New Folder", "Folder name:", QLineEdit.Normal, "",
        )
        if not ok or not name.strip():
            return
        try:
            self._repo.create_folder(name)
        except ValueError as e:
            QMessageBox.warning(self, "Could not create folder", str(e))
            return
        self._reload_sidebar(self._account.iri if self._account else None)

    def _on_new_folder_and_assign(self, account_iri: str) -> None:
        """Create a folder via prompt and immediately move the right-clicked
        account into it. Used from the account context menu's 'New Folder…'
        shortcut so the user doesn't have to do this in two steps."""
        name, ok = QInputDialog.getText(
            self, "New Folder", "Folder name:", QLineEdit.Normal, "",
        )
        if not ok or not name.strip():
            return
        try:
            folder = self._repo.create_folder(name)
            acct = self._repo.get_account_by_iri(account_iri)
            if acct is not None:
                self._repo.set_account_folder(acct.id, folder.id)
        except ValueError as e:
            QMessageBox.warning(self, "Could not create folder", str(e))
            return
        self._reload_sidebar(account_iri)

    def _on_rename_folder(self, folder_id: int, current_name: str) -> None:
        new_name, ok = QInputDialog.getText(
            self, "Rename Folder", "New name:", QLineEdit.Normal, current_name,
        )
        if not ok or new_name.strip() == current_name:
            return
        try:
            self._repo.rename_folder(folder_id, new_name)
        except ValueError as e:
            QMessageBox.warning(self, "Could not rename", str(e))
            return
        self._reload_sidebar(self._account.iri if self._account else None)

    def _on_delete_folder(self, folder_id: int, folder_name: str) -> None:
        confirm = QMessageBox.question(
            self, "Confirm delete",
            f"Delete folder {folder_name!r}?\n\nAccounts inside the folder "
            f"will move back to the sidebar root — no accounts or "
            f"transactions are deleted.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._repo.delete_folder(folder_id)
        except Exception as e:
            QMessageBox.critical(self, "Could not delete folder", str(e))
            return
        self._reload_sidebar(self._account.iri if self._account else None)

    def _on_move_folder(self, folder_id: int, direction: int) -> None:
        try:
            self._repo.move_folder(folder_id, direction)
        except Exception as e:
            QMessageBox.critical(self, "Could not reorder folder", str(e))
            return
        self._reload_sidebar(self._account.iri if self._account else None)

    def _move_account_to_folder(
        self, account_iri: str, folder_id: Optional[int],
    ) -> None:
        acct = self._repo.get_account_by_iri(account_iri)
        if acct is None:
            return
        try:
            self._repo.set_account_folder(acct.id, folder_id)
        except Exception as e:
            QMessageBox.critical(self, "Could not move account", str(e))
            return
        self._reload_sidebar(account_iri)

    def _refresh_sidebar_balances(self) -> None:
        """Reload the sidebar after a balance-affecting operation (txn
        add/delete, import). Preserves account selection; folder
        expansion is reset (acceptable for v1 — see ADR-015 backlog)."""
        self._reload_sidebar(self._account.iri if self._account else None)

    def _reload_sidebar(self, select_iri: Optional[str]) -> None:
        """Reload the sidebar's account + folder list and pick a target row.

        If ``select_iri`` is provided and still exists, it becomes the
        current row. Otherwise the first account is selected, or
        All Transactions if no accounts remain. Balances are recomputed
        each time so the sidebar reflects the current ledger state.
        """
        accounts = self._repo.list_accounts()
        folders = self._repo.list_folders()
        balances = self._repo.compute_account_balances()
        self._sidebar.reload(accounts, folders, balances)
        if select_iri is not None:
            self._select_account_in_sidebar(select_iri)
        elif accounts:
            self._select_account_in_sidebar(accounts[0].iri)
        else:
            self._sidebar.select_all_transactions()
            self._show_all_transactions()

    def _on_sidebar_context_menu(self, pos) -> None:
        item = self._sidebar.itemAt(pos)
        if item is None:
            # Empty area — offer New Account + New Folder.
            menu = QMenu(self._sidebar)
            menu.addAction(self._new_account_action)
            menu.addAction(self._new_folder_action)
            menu.exec(self._sidebar.viewport().mapToGlobal(pos))
            return
        kind = item.data(0, KIND_ROLE)
        if kind == "all":
            menu = QMenu(self._sidebar)
            menu.addAction(self._new_account_action)
            menu.addAction(self._new_folder_action)
            menu.exec(self._sidebar.viewport().mapToGlobal(pos))
            return
        if kind == "folder":
            folder_id = item.data(0, Qt.UserRole)
            folder_name = item.text(0)
            menu = QMenu(self._sidebar)
            rename_act = menu.addAction("&Rename Folder…")
            rename_act.triggered.connect(
                lambda: self._on_rename_folder(folder_id, folder_name)
            )
            menu.addSeparator()
            up_act = menu.addAction("Move &Up")
            up_act.triggered.connect(lambda: self._on_move_folder(folder_id, -1))
            down_act = menu.addAction("Move &Down")
            down_act.triggered.connect(lambda: self._on_move_folder(folder_id, +1))
            menu.addSeparator()
            menu.addAction(self._new_folder_action)
            menu.addAction(self._new_account_action)
            menu.addSeparator()
            delete_act = menu.addAction("&Delete Folder…")
            delete_act.triggered.connect(
                lambda: self._on_delete_folder(folder_id, folder_name)
            )
            menu.exec(self._sidebar.viewport().mapToGlobal(pos))
            return
        if kind == "account":
            iri = item.data(0, Qt.UserRole)
            # Make this the current selection so Edit/Delete operate on it.
            self._select_account_in_sidebar(iri)
            menu = QMenu(self._sidebar)
            menu.addAction(self._new_account_action)
            menu.addAction(self._edit_account_action)
            menu.addSeparator()
            move_menu = menu.addMenu("&Move to Folder")
            self._populate_move_to_folder_menu(move_menu, iri)
            menu.addSeparator()
            menu.addAction(self._delete_account_action)
            menu.exec(self._sidebar.viewport().mapToGlobal(pos))
            return

    def _populate_move_to_folder_menu(self, menu: "QMenu", account_iri: str) -> None:
        """Build the Move to Folder ▸ submenu for the right-clicked account.
        Lists existing folders, a 'No folder' option to move out, and a
        'New Folder…' shortcut that creates one and assigns the account."""
        folders = self._repo.list_folders()
        acct = self._repo.get_account_by_iri(account_iri)
        current_folder_id = acct.folder_id if acct is not None else None

        no_folder_act = menu.addAction("(No folder)")
        no_folder_act.setEnabled(current_folder_id is not None)
        no_folder_act.triggered.connect(
            lambda: self._move_account_to_folder(account_iri, None)
        )

        if folders:
            menu.addSeparator()
            for f in folders:
                act = menu.addAction(f.name)
                act.setEnabled(f.id != current_folder_id)
                fid = f.id  # bind for the closure
                act.triggered.connect(
                    lambda checked=False, target=fid: self._move_account_to_folder(
                        account_iri, target,
                    )
                )

        menu.addSeparator()
        new_act = menu.addAction("&New Folder…")
        new_act.triggered.connect(
            lambda: self._on_new_folder_and_assign(account_iri)
        )

    # ── manage menus ──

    def _on_manage_payees(self) -> None:
        dialog = PayeesDialog(self._repo, parent=self)
        # Reload register data whenever a payee operation changes the world,
        # so payee names on visible rows reflect the edits without waiting
        # for the dialog to close.
        dialog.payees_changed.connect(self._model.reload)
        dialog.exec()

    def _on_manage_categories(self) -> None:
        dialog = CategoriesDialog(self._repo, parent=self)
        # Category edits can change rows' category_name *and* category_id
        # (merge/delete reassigns to Uncategorised), and the delegate +
        # filter combo are both built off the cached category list — so
        # refresh the lot whenever the dialog changes anything.
        dialog.categories_changed.connect(self._refresh_categories_view)
        dialog.exec()

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
