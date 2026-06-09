"""Register main window — top-level Qt surface for the desktop app.

Multi-account: a left-hand sidebar lists accounts with "All transactions"
on top. Selecting an account swaps the model and column layout; selecting
"All transactions" shows the cross-account aggregate (Account column added,
Balance column hidden — see project-all-transactions-view in memory).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.import_engine.import_service import ImportService
from mfl_desktop.ui.account_dialog import AccountDialog
from mfl_desktop.ui.account_summary_window import AccountSummaryWindow
from mfl_desktop.ui.budget_window import BudgetWindow
from mfl_desktop.ui.bulk_edit_dialog import BulkEditDialog
from mfl_desktop.ui.categories_dialog import CategoriesDialog
from mfl_desktop.ui.csv_mapping_dialog import CsvMappingDialog
from mfl_desktop.ui.currencies_dialog import CurrenciesDialog
from mfl_desktop.ui.securities_dialog import SecuritiesDialog
from mfl_desktop.ui.transfer_reconcile_dialog import TransferReconcileDialog
from mfl_desktop.ui.delegates import (
    CategoryTypeaheadDelegate,
    PayeeTypeaheadDelegate,
    StatusDelegate,
)
from mfl_desktop.ui.filter_proxy import TransactionFilterProxy
from mfl_desktop.ui.payees_dialog import PayeesDialog
from mfl_desktop.ui.register_model import TransactionTableModel
from mfl_desktop.ui.schedule_dialog import ScheduleDialog, ScheduleSeed
from mfl_desktop.ui.schedules_dialog import SchedulesDialog
from mfl_desktop.ui.sidebar import KIND_ROLE, Sidebar
from mfl_desktop.ui.statements_window import StatementsWindow
from mfl_desktop.ui.net_worth_window import NetWorthWindow
from mfl_desktop.ui.new_report_dialog import NewReportDialog
from mfl_desktop.ui.spending_report_window import SpendingReportWindow
from mfl_desktop.reports.filters import (
    TYPE_INCOME_EXPENSE,
    TYPE_NET_WORTH,
    TYPE_SANKEY,
    TYPE_SPENDING_OVER_TIME,
)
from mfl_desktop.ui.transaction_dialog import NewTransactionDialog
from mfl_desktop.ui.transfer_destination_dialog import (
    TransferDestinationDialog,
    no_other_accounts_message,
)
from mfl_desktop.ui.transfer_match_dialogs import (
    BulkRowAnalysis,
    BulkTransferReviewDialog,
    TransferMatchConfirmDialog,
    TransferMatchPickerDialog,
)

STATUSES = ("Pending", "Uncleared", "Cleared", "Reconciled")

# ADR-041: register date-window presets — (label, key). The chosen window is
# turned into an inclusive lower bound on posted_date and pushed into the
# Repository query, so the default view fetches and sorts only recent rows
# instead of the full account history. Default = rolling quarter (90 days).
_WINDOW_PRESETS = [
    ("Last 30 days",   "30d"),
    ("Rolling quarter", "90d"),
    ("Year to date",   "ytd"),
    ("All",            "all"),
]
_DEFAULT_WINDOW_KEY = "90d"

# Mirror of the sidebar's currency-symbol table so the status-bar Net
# matches the in-app convention. Unknown currencies fall back to no
# symbol — the signed magnitude is still readable.
_CURRENCY_SYMBOLS: dict[str, str] = {
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
    "JPY": "¥",
}

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
    # Investment register (ADR-043)
    "action":          90,
    "security_symbol": 80,
    "security_name":   260,
    "quantity":        100,
    "price":           100,
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
        # ADR-041: current register date-window key; resolved to a posted_date
        # lower bound by _current_since(). Set before the initial selection
        # builds a model so the first view is already windowed.
        self._window_key = _DEFAULT_WINDOW_KEY

        self.resize(1360, 760)

        # ── sidebar + filter bar ──

        accounts = repo.list_accounts()
        folders = repo.list_folders()
        # Sidebar shows each account's worth — market value for investment
        # accounts (cash + holdings), cash for everything else (ADR-044).
        balances = repo.compute_account_values()
        reports = repo.list_reports()
        report_folders = repo.list_report_folders()
        self._sidebar = Sidebar(
            accounts, folders, balances,
            reports=reports, report_folders=report_folders,
        )
        self._sidebar.selection_changed.connect(self._on_sidebar_change)
        self._sidebar.setContextMenuPolicy(Qt.CustomContextMenu)
        self._sidebar.customContextMenuRequested.connect(
            self._on_sidebar_context_menu
        )
        # Double-click an account row to open its Account Summary screen
        # (ADR-033). Folder rows and 'All transactions' don't summary.
        self._sidebar.itemDoubleClicked.connect(self._on_sidebar_double_click)

        # Single-instance-per-account registry — opening an account that
        # already has a summary window raises the existing one (ADR-033).
        self._account_summary_wins: dict[int, AccountSummaryWindow] = {}

        # Single-instance-per-saved-report registry (ADR-039). A bare-
        # opened report window (no saved-id) lives in self._bare_report_wins
        # keyed by type, so the Reports-menu entry stays singleton per
        # type without conflicting with the saved-id windows.
        self._saved_report_wins: dict[int, SpendingReportWindow] = {}
        self._bare_report_wins: dict[str, SpendingReportWindow] = {}

        search = QLineEdit()
        search.setPlaceholderText("Search payee, memo, amount, or date…")
        search.textChanged.connect(lambda s: self._proxy.set_search(s))

        # ADR-041: date-window selector. Default seeded from self._window_key.
        self._window_combo = QComboBox()
        for label, key in _WINDOW_PRESETS:
            self._window_combo.addItem(label, key)
        self._window_combo.setCurrentIndex(
            next(
                i for i, (_, k) in enumerate(_WINDOW_PRESETS)
                if k == self._window_key
            )
        )
        self._window_combo.currentIndexChanged.connect(self._on_window_change)

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
        filter_bar.addWidget(QLabel("Show:"))
        filter_bar.addWidget(self._window_combo)
        filter_bar.addSpacing(12)
        filter_bar.addWidget(QLabel("Status:"))
        filter_bar.addWidget(status_combo)
        filter_bar.addSpacing(12)
        filter_bar.addWidget(QLabel("Category:"))
        filter_bar.addWidget(self._category_combo, stretch=1)
        filter_bar.addSpacing(12)
        # Reconcile button — opens the statement history for the current
        # account (ADR-040). Disabled in all-transactions mode, in lockstep
        # with the Account → Reconcile… menu action.
        self._reconcile_btn = QPushButton("Reconcile…")
        self._reconcile_btn.clicked.connect(self._on_reconcile_for_selection)
        filter_bar.addWidget(self._reconcile_btn)

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
        splitter.setSizes([360, 1000])
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

        # Auto-post any schedules that have come due since the last launch —
        # see ADR-023. Runs against the freshly-opened DB only; the user's
        # variable-amount and manual schedules are left for them to action
        # via Manage ▸ Schedules.
        self._run_auto_post_sweep()

    # ── sidebar plumbing ──

    def _select_account_in_sidebar(self, account_iri: str) -> None:
        if not self._sidebar.select_account_by_iri(account_iri):
            # Account not in sidebar (deleted?) — fall back to all-transactions.
            self._sidebar.select_all_transactions()
            self._show_all_transactions()

    def _on_sidebar_change(self, kind: str, payload) -> None:
        """Dispatch a new sidebar selection to the right surface.

        The sidebar emits one of three kinds (ADR-039):
        - ``"all_transactions"`` → cross-account register view
        - ``"account"`` (payload = account IRI) → single-account register
        - ``"report"`` (payload = report id int) → open the saved report
          window for that row (singleton per report id)
        """
        if kind == "all_transactions":
            self._show_all_transactions()
            return
        if kind == "account":
            if isinstance(payload, str):
                self._show_account(payload)
            return
        if kind == "report":
            if isinstance(payload, int):
                self._open_saved_report(payload)
            return

    # ── view modes ──

    def _show_account(self, account_iri: str) -> None:
        acct = self._repo.get_account_by_iri(account_iri)
        if acct is None:
            self._show_all_transactions()
            return
        self._account = acct
        self._update_window_title()
        self._set_model(TransactionTableModel(
            self._repo, account_id=acct.id, since=self._current_since(),
            invest=(acct.family == "investment"),
        ))
        # The category combo lists only the categories actually used in
        # the current view — rebuild it now that _account has flipped.
        self._populate_category_combo()
        self._import_action.setEnabled(True)
        self._import_action.setToolTip("Import OFX / QFX / QIF / CSV into this account")
        self._set_account_action_state(account_selected=True)

    def _show_all_transactions(self) -> None:
        self._account = None
        self._update_window_title()
        self._set_model(TransactionTableModel(
            self._repo, account_id=None, since=self._current_since(),
        ))
        # See _show_account: the category combo is per-view.
        self._populate_category_combo()
        self._import_action.setEnabled(False)
        self._import_action.setToolTip(
            "Select an account in the sidebar to import into it"
        )
        self._set_account_action_state(account_selected=False)

    def _current_since(self) -> Optional[str]:
        """Resolve the active window key (ADR-041) to an inclusive
        'YYYY-MM-DD' lower bound on posted_date, or None for full history."""
        key = self._window_key
        if key == "all":
            return None
        today = date.today()
        if key == "ytd":
            return f"{today.year:04d}-01-01"
        days = {"30d": 30, "90d": 90}.get(key, 90)
        return (today - timedelta(days=days)).isoformat()

    def _on_window_change(self, index: int) -> None:
        """Date-window combo changed — re-window the current model in place.

        The column layout is identical across windows, so set_since just
        resets the rows (no delegate/​column-width teardown). The proxy
        re-sorts on the model reset, keeping the active sort column."""
        self._window_key = self._window_combo.itemData(index)
        self._model.set_since(self._current_since())

    def _update_window_title(self) -> None:
        filename = self._repo.db_path.name
        if self._account is None:
            suffix = "All transactions"
        else:
            suffix = f"{self._account.name}  ·  {self._account.currency}"
        self.setWindowTitle(f"My Financial Life — {filename} — {suffix}")

    def _set_account_action_state(self, account_selected: bool) -> None:
        """Enable Edit/Delete/Summary only when a specific account is being viewed."""
        if hasattr(self, "_edit_account_action"):
            self._edit_account_action.setEnabled(account_selected)
            self._delete_account_action.setEnabled(account_selected)
        if hasattr(self, "_account_summary_action"):
            self._account_summary_action.setEnabled(account_selected)
        if hasattr(self, "_reconcile_action"):
            self._reconcile_action.setEnabled(account_selected)
        if hasattr(self, "_reconcile_btn"):
            self._reconcile_btn.setEnabled(account_selected)

    def _set_model(self, model: TransactionTableModel) -> None:
        """Swap the source model and reattach delegates + column widths for
        the new column layout.

        Closes any inline editor (Payee / Category typeahead, Status
        combo, etc.) first so it doesn't try to commit against the
        about-to-be-replaced view/model and surface the Qt warning
        ``commitData called with an editor that does not belong to
        this view``. Most commonly hit when the user clicks a different
        sidebar account while a cell is still being edited, or when a
        big delete triggers a reload mid-edit."""
        focused = QApplication.focusWidget()
        if focused is not None and self._table.isAncestorOf(focused):
            # Discard the in-flight edit rather than commit it — the
            # user has navigated away (clicked a different account,
            # deleted an account, etc.); committing now would write to
            # whichever cell happened to have focus, which is rarely
            # what they intended.
            self._table.closeEditor(focused, QAbstractItemDelegate.NoHint)
        self._model = model
        self._proxy.setSourceModel(self._model)
        self._model.reload()
        # Inline category edits trigger the transfer-conversion prompt
        # via dataChanged (ADR-020). Connecting per-model is safe — the
        # old model is dropped when self._model is reassigned, taking
        # its connection with it.
        self._model.dataChanged.connect(self._on_model_data_changed)
        # ADR-040: confirm before an inline edit lands on a reconciled row.
        self._model.reconciled_edit_guard = self._confirm_reconciled_edit

        col_index = {name: i for i, (_, name, _) in enumerate(self._model.COLUMNS)}
        # Clear all delegates then reattach where applicable, since column
        # positions differ between modes.
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
                    self._on_create_category_inline,
                    self._table,
                ),
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
        parts = [f"Showing {visible:,} of {total:,} transactions{suffix}"]
        # Net of the visible rows is meaningful inside a single-currency
        # view; the cross-account view mixes currencies so the bare sum
        # would be misleading — skip it there. Cheap to walk 1.3k rows
        # in Python; if this grows we move it into the proxy.
        if self._account is not None and visible > 0:
            net = Decimal("0.00")
            for i in range(visible):
                src = self._proxy.mapToSource(self._proxy.index(i, 0))
                net += self._model.row_at(src.row()).amount
            parts.append(self._format_net(net, self._account.currency))
        self.statusBar().showMessage("  ·  ".join(parts))

    @staticmethod
    def _format_net(amount: Decimal, currency: str) -> str:
        symbol = _CURRENCY_SYMBOLS.get(currency, "")
        body = f"{abs(amount):,.2f}"
        if amount < 0:
            return f"Net: -{symbol}{body}"
        return f"Net: {symbol}{body}"

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

        self._account_summary_action = QAction("Account &Summary…", self)
        self._account_summary_action.setShortcut(QKeySequence("Ctrl+I"))
        self._account_summary_action.triggered.connect(
            self._on_open_account_summary_for_selection
        )
        account_menu.addAction(self._account_summary_action)
        # Expose on the window so the shortcut fires while the table has focus.
        self.addAction(self._account_summary_action)

        # ADR-040: Reconcile… opens the statement history for the selected
        # account. Ctrl+Alt+R avoids Ctrl+B (Budget) and Ctrl+Shift+R
        # (Reconcile Transfers).
        self._reconcile_action = QAction("&Reconcile…", self)
        self._reconcile_action.setShortcut(QKeySequence("Ctrl+Alt+R"))
        self._reconcile_action.triggered.connect(
            self._on_reconcile_for_selection
        )
        account_menu.addAction(self._reconcile_action)
        self.addAction(self._reconcile_action)

        account_menu.addSeparator()

        self._new_account_action = QAction("&New Account…", self)
        self._new_account_action.triggered.connect(self._on_new_account)
        account_menu.addAction(self._new_account_action)

        self._edit_account_action = QAction("&Edit Account…", self)
        self._edit_account_action.triggered.connect(self._on_edit_account)
        account_menu.addAction(self._edit_account_action)

        self._delete_account_action = QAction("&Delete Account…", self)
        self._delete_account_action.triggered.connect(self._on_delete_account)
        account_menu.addAction(self._delete_account_action)

        # Edit/Delete/Summary are only meaningful when a specific account
        # is selected; state is kept in sync by _set_account_action_state
        # on view changes.
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

        self._manage_schedules_action = QAction("&Schedules…", self)
        self._manage_schedules_action.triggered.connect(self._on_manage_schedules)
        manage_menu.addAction(self._manage_schedules_action)

        self._manage_currencies_action = QAction("Cu&rrencies…", self)
        self._manage_currencies_action.triggered.connect(self._on_manage_currencies)
        manage_menu.addAction(self._manage_currencies_action)

        self._manage_securities_action = QAction("Se&curities…", self)
        self._manage_securities_action.setToolTip(
            "Investment prices — Tiingo API key, refresh, and manual prices"
        )
        self._manage_securities_action.triggered.connect(self._on_manage_securities)
        manage_menu.addAction(self._manage_securities_action)

        self._reconcile_transfers_action = QAction(
            "&Reconcile Transfers…", self,
        )
        self._reconcile_transfers_action.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self._reconcile_transfers_action.triggered.connect(
            self._on_reconcile_transfers,
        )
        manage_menu.addAction(self._reconcile_transfers_action)
        self.addAction(self._reconcile_transfers_action)

        reports_menu = self.menuBar().addMenu("&Reports")

        self._spending_report_action = QAction("&Spending Over Time…", self)
        self._spending_report_action.triggered.connect(self._on_spending_report)
        reports_menu.addAction(self._spending_report_action)

        self._net_worth_action = QAction("&Net Worth…", self)
        self._net_worth_action.triggered.connect(self._on_net_worth)
        reports_menu.addAction(self._net_worth_action)

        budget_menu = self.menuBar().addMenu("&Budget")

        self._budget_action = QAction("&Open Budget…", self)
        self._budget_action.setShortcut(QKeySequence("Ctrl+B"))
        self._budget_action.triggered.connect(self._on_open_budget)
        budget_menu.addAction(self._budget_action)
        # Expose on the window so the shortcut fires while the table has focus.
        self.addAction(self._budget_action)

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

        # ADR-020: a transfer-kind category turns this into a transfer —
        # prompt for the destination, then create both halves via
        # create_transfer. ADR-035 amendment 2026-06-07: when the
        # destination account is a different currency, the same dialog
        # also collects the amount that hits the destination side, so
        # cross-currency transfers don't require a stored FX rate.
        if self._category_kind(values.category_id) == "transfer":
            source_acct = self._repo.get_account_by_id(values.account_id)
            if source_acct is None:
                return
            others = [
                a for a in self._repo.list_accounts()
                if a.id != values.account_id
            ]
            if not others:
                no_other_accounts_message(self)
                return
            dest_dialog = TransferDestinationDialog(
                repo=self._repo,
                source_account=source_acct,
                source_magnitude=abs(values.amount),
                source_signed_display=values.amount,
                posted_date=values.posted_date,
                exclude_account_ids={values.account_id},
                title="New transfer",
                intro=(
                    "This category is a transfer category — which account "
                    "is the other side?"
                ),
                parent=self,
            )
            if dest_dialog.exec() != QDialog.Accepted:
                return
            choice = dest_dialog.values()
            if choice is None:
                return
            # Direction is encoded in the signed amount: negative = "money
            # out from this account" → dialog account is the source;
            # positive = inflow → dialog account is the destination.
            # The dialog's `other_amount` is always the magnitude on the
            # *other* side; map it to create_transfer's amount / to_amount
            # based on direction so the source side stays the truth-of-
            # the-source's-statement.
            this_mag = abs(values.amount)
            if values.amount < 0:
                from_id, to_id = values.account_id, choice.account_id
                source_amount = this_mag
                target_to_amount = choice.other_amount
            else:
                from_id, to_id = choice.account_id, values.account_id
                # Inflow: source side is the other account, so its
                # magnitude is the dialog's other_amount; the dialog
                # account's amount becomes the to_amount.
                source_amount = (
                    choice.other_amount
                    if choice.other_amount is not None
                    else this_mag
                )
                target_to_amount = this_mag
            try:
                self._repo.create_transfer(
                    from_account_id=from_id,
                    to_account_id=to_id,
                    posted_date=values.posted_date,
                    amount=source_amount,
                    category_id=values.category_id,
                    memo=values.memo,
                    status=values.status,
                    to_amount=target_to_amount,
                )
            except Exception as e:
                QMessageBox.critical(
                    self, "Could not save transfer",
                    f"The transfer was not saved:\n\n{e}",
                )
                return
            self._model.reload()
            self._refresh_sidebar_balances()
            self.statusBar().showMessage("Transfer recorded", 4000)
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

    def _on_model_data_changed(self, top_left, _bottom_right, _roles) -> None:
        """Detect inline edits that need post-processing:

        - Category set to a transfer-kind value → pop the destination
          prompt (ADR-020).
        - Amount changed → reload the model so running balances
          recompute, and refresh sidebar balances.

        Other edits — payee, status, memo — pass through without action."""
        col_idx = top_left.column()
        if col_idx < 0 or col_idx >= len(self._model.COLUMNS):
            return
        col_name = self._model.COLUMNS[col_idx][1]
        if col_name == "amount":
            # Running balance and sidebar totals are stale after an
            # amount edit; reload picks up the new running balance from
            # the Repository (computed in list order) and the sidebar
            # refresh re-sums account totals.
            self._model.reload()
            self._refresh_sidebar_balances()
            return
        if col_name != "category_name":
            return
        row = self._model.row_at(top_left.row())
        if row.transfer_id is not None:
            return
        if self._category_kind(row.category_id) != "transfer":
            return
        # ADR-035 amendment 2026-06-07: open the destination-amount-aware
        # dialog so the cross-currency case (USD txn marked as transfer
        # to a GBP account) doesn't require a stored FX rate. The dialog
        # collapses to a plain account picker when both currencies match.
        source_acct = self._repo.get_account_by_id(row.account_id)
        if source_acct is None:
            return
        others = [
            a for a in self._repo.list_accounts() if a.id != row.account_id
        ]
        if not others:
            no_other_accounts_message(self)
            return
        dest_dialog = TransferDestinationDialog(
            repo=self._repo,
            source_account=source_acct,
            source_magnitude=abs(row.amount),
            source_signed_display=row.amount,
            posted_date=row.posted_date,
            exclude_account_ids={row.account_id},
            title="New transfer",
            intro=(
                "This category is a transfer category — which account "
                "is the other side?"
            ),
            parent=self,
        )
        if dest_dialog.exec() != QDialog.Accepted:
            # User cancelled. Row is left with the transfer-kind category
            # but no partner — a recoverable rough edge (re-setting the
            # category triggers the prompt again).
            return
        choice = dest_dialog.values()
        if choice is None:
            return
        other_id = choice.account_id

        # ADR-036: run the matcher before manufacturing a partner.
        # The other side may already exist on the destination account
        # (very common after importing both ends of a transfer separately
        # at setup time). If a candidate is offered and the user accepts
        # it, we link instead of creating a duplicate inflow.
        chosen = self._offer_transfer_match(
            source_row=row, other_account_id=other_id,
        )
        if chosen == "cancelled":
            return
        try:
            if isinstance(chosen, int):
                # User accepted an existing candidate; chosen is its txn id.
                # The candidate's existing amount carries the other-side
                # truth, so the explicit to_amount we just collected is
                # not propagated here — link_transfer back-derives the
                # rate from the two stored amounts.
                self._repo.link_transfer(
                    source_txn_id=row.id,
                    candidate_txn_id=chosen,
                    category_id=row.category_id,
                )
                message = "Linked to existing transaction"
            else:
                # convert_to_transfer's to_amount is "magnitude on the
                # partner side", which is exactly what the dialog returns
                # as other_amount (None for same-currency).
                self._repo.convert_to_transfer(
                    txn_id=row.id,
                    other_account_id=other_id,
                    to_amount=choice.other_amount,
                )
                message = "Transfer partner created"
        except Exception as e:
            QMessageBox.critical(
                self, "Could not create transfer partner", str(e),
            )
            return
        self._model.reload()
        self._refresh_sidebar_balances()
        self.statusBar().showMessage(message, 4000)

    # ── inline category create (ADR-022) ──

    def _on_create_category_inline(self, name: str) -> Optional[int]:
        """Confirm-and-create a brand-new top-level category from the
        register's category typeahead delegate.

        Defaults per ADR-022: top-level (parent_id=None), kind='expense',
        source='user'. A single Yes/No confirm guards against typos —
        creating a junk category from a misspelled lookup is more painful
        to clean up than the one extra keystroke costs. On Yes we refresh
        the cached choice list, the typeahead delegate, and the filter
        combo via _refresh_categories_view so the new entry is visible
        everywhere immediately.

        Returns the new category id, or None if the user cancelled or the
        creation failed."""
        clean = (name or "").strip()
        if not clean:
            return None
        confirm = QMessageBox.question(
            self, "Create category?",
            f"No category named {clean!r} exists.\n\n"
            f"Create it as a new top-level expense category?\n"
            f"(You can move it under a parent or change its kind later "
            f"from Manage ▸ Categories.)",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return None
        try:
            new_id = self._repo.create_category(
                clean, parent_id=None, kind="expense", source="user",
            )
        except ValueError as e:
            # Sibling-name collision (race with another window or with a
            # category that exists but wasn't in the typeahead because the
            # delegate's snapshot is stale) — surface the reason rather
            # than silently doing nothing.
            QMessageBox.warning(self, "Could not create category", str(e))
            return None
        # Lightweight refresh only: update the cached list + filter combo so
        # the dataChanged hook and the next filter use see the new category.
        # Skip the full _refresh_categories_view here — it resets the model,
        # which would invalidate the QModelIndex the delegate is about to
        # commit into. The typeahead delegate re-reads choices fresh on
        # its next createEditor, so no delegate rebind is needed either.
        self._reload_category_cache()
        self.statusBar().showMessage(
            f"Created category {clean!r} (expense, top-level)", 6000,
        )
        return new_id

    def _reload_category_cache(self) -> None:
        """Refresh the cached category list and the filter-bar combo without
        touching the model or the typeahead delegate. Safe to call from
        inside a delegate's setModelData."""
        self._categories = self._repo.list_categories_flat()
        self._populate_category_combo()

    # ── transfer helpers (category-driven, ADR-020) ──

    def _category_kind(self, category_id: int) -> Optional[str]:
        """Look up a category's kind from the window's cached list, with
        a Repository fallback when the cache misses.

        The cache misses (and would silently return None) whenever the
        category was created or kind-changed since the window cached
        ``self._categories`` — e.g. a kind change made via the CLI, a
        category dialog whose ``categories_changed`` signal hadn't yet
        propagated, or any future code path that mutates kind without
        refreshing the window cache. The DB fallback keeps the
        transfer-prompt trigger correct in every case; on hit we
        re-warm the cache so subsequent lookups are fast again."""
        for c in self._categories:
            if c.id == category_id:
                return c.kind
        kind = self._repo.get_category_kind(category_id)
        if kind is not None:
            # Cache was stale — pull fresh so dependent surfaces
            # (filter combo, dialogs) catch up too.
            self._reload_category_cache()
        return kind

    def _offer_transfer_match(self, *, source_row, other_account_id: int):
        """Run the transfer matcher (ADR-036) for one source row.

        Returns one of:

        - ``"cancelled"`` — user dismissed the picker; caller should bail
          out of the whole flow.
        - an ``int`` — txn id of the candidate the user chose to link to.
        - ``None`` — fall through to today's create-partner behaviour
          (zero candidates, or user picked "Create new" in the picker).

        The source's account name / amount / date / payee come from the
        register row; the source's currency is looked up via the
        Repository. The matcher applies the configured ±N-day window and
        FX tolerance from ``setting`` (defaults 3 days / 1%).
        """
        try:
            candidates = self._repo.find_transfer_candidates(
                source_txn_id=source_row.id,
                other_account_id=other_account_id,
            )
        except Exception as e:
            # If the matcher itself errors (e.g. missing FX rate for a
            # cross-currency case), fall back silently to create-partner
            # — the user still gets a working transfer; we surface the
            # reason via the status bar so it's not invisible.
            self.statusBar().showMessage(
                f"Could not search for matches: {e}", 6000,
            )
            return None
        if not candidates:
            return None
        src_currency = (
            self._repo.get_account_currency(source_row.account_id) or ""
        )
        if len(candidates) == 1:
            dlg = TransferMatchConfirmDialog(
                candidate=candidates[0],
                source_account=source_row.account_name,
                source_amount=source_row.amount,
                source_currency=src_currency,
                source_date=source_row.posted_date,
                source_payee=source_row.payee_name,
                parent=self,
            )
        else:
            dlg = TransferMatchPickerDialog(
                candidates=candidates,
                source_account=source_row.account_name,
                source_amount=source_row.amount,
                source_currency=src_currency,
                source_date=source_row.posted_date,
                source_payee=source_row.payee_name,
                parent=self,
            )
        if dlg.exec() != QDialog.Accepted:
            return "cancelled"
        picked = dlg.result_candidate()
        return picked.txn_id if picked is not None else None

    def _prompt_destination_account(
        self,
        *,
        exclude_account_ids: set[int],
        title: str = "Pick destination account",
        message: str = "Transfer to which account?",
    ) -> Optional[int]:
        """Modal account picker for the destination half of a transfer.

        Returns the chosen account id, or None if the user cancelled or
        there are no candidate accounts. Used by the new-transaction
        save path, the inline-category-edit hook, and bulk edit."""
        accounts = self._repo.list_accounts()
        candidates = [a for a in accounts if a.id not in exclude_account_ids]
        if not candidates:
            QMessageBox.information(
                self, "No other account",
                "You need at least one other account to record a transfer.",
            )
            return None
        names = [f"{a.name}  ·  {a.currency}" for a in candidates]
        choice, ok = QInputDialog.getItem(
            self, title, message, names, 0, False,
        )
        if not ok:
            return None
        for label, acct in zip(names, candidates):
            if label == choice:
                return acct.id
        return None

    def _on_delete_transactions(self) -> None:
        ids = self._selected_txn_ids()
        if not ids:
            return
        # If any selected row is part of a transfer, both halves will be
        # removed — surface that in the confirmation so the user isn't
        # surprised by a sibling vanishing from a different account.
        expanded = self._repo.expand_transfer_partners(ids)
        partner_count = len(expanded) - len(ids)
        if len(expanded) > 1:
            msg = f"Delete {len(expanded)} transactions?"
        else:
            msg = "Delete this transaction?"
        if partner_count > 0:
            msg += (
                f"\n\n{partner_count} of these are linked transfer "
                f"partner{'s' if partner_count != 1 else ''} that will be "
                f"removed automatically — both halves of a transfer "
                f"always go together."
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

        # ADR-027 — Create Schedule From Transaction. Only meaningful for a
        # single seed row; seed-from-many would have to reconcile differing
        # accounts / categories / amounts and isn't worth the UX.
        if len(ids) == 1:
            menu.addSeparator()
            sched_act = menu.addAction("Create Schedule From Transaction…")
            sched_act.triggered.connect(self._on_create_schedule_from_txn)

        if len(ids) >= 2:
            menu.addSeparator()
            bulk_act = menu.addAction(f"Bulk Edit {len(ids)} Transactions…")
            bulk_act.triggered.connect(self._on_bulk_edit)

        menu.addSeparator()
        delete_act = menu.addAction(
            f"Delete {len(ids)} Transactions" if len(ids) > 1
            else "Delete Transaction"
        )
        delete_act.setEnabled(bool(ids))
        delete_act.triggered.connect(self._on_delete_transactions)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_create_schedule_from_txn(self) -> None:
        """Right-click verb — seed a New Schedule dialog from one selected
        txn (ADR-027). The dialog's "Next occurrence" field is pre-filled
        with the first future date at the default cadence (step forward
        from the source txn until > today). The schedule's anchor and
        next-due both store that future date; the source txn isn't itself
        used as the anchor.
        """
        ids = self._selected_txn_ids()
        if len(ids) != 1:
            return

        # Locate the source TransactionRow — single-row select means a
        # single proxy index; map to source and read it from the model.
        selection = self._table.selectionModel()
        if selection is None:
            return
        rows = selection.selectedRows()
        if len(rows) != 1:
            return
        source_idx = self._proxy.mapToSource(rows[0])
        if not source_idx.isValid():
            return
        txn = self._model.row_at(source_idx.row())

        # For a transfer-half row, pre-fill destination = partner's account.
        transfer_to_id: Optional[int] = None
        if txn.transfer_id:
            transfer_to_id = self._repo.get_transfer_partner_account_id(txn.id)

        # Compute the next future occurrence at the default cadence —
        # step forward from the source txn date by one cadence period at
        # a time until the result is strictly after today. A May 25 txn
        # viewed on June 6 settles after one step (June 25); a txn from
        # months ago iterates as many times as needed.
        default_cadence = "monthly"
        today_iso = date.today().isoformat()
        next_occ = txn.posted_date
        try:
            while next_occ <= today_iso:
                next_occ = self._repo.compute_next_due_date(
                    anchor_date=txn.posted_date,
                    cadence=default_cadence,
                    current_due=next_occ,
                )
        except ValueError as e:
            QMessageBox.critical(
                self, "Could not compute next occurrence", str(e),
            )
            return

        seed = ScheduleSeed(
            account_id=txn.account_id,
            payee_name=txn.payee_name or "",
            category_id=txn.category_id,
            transfer_to_account_id=transfer_to_id,
            amount=txn.amount,            # already signed
            anchor_date=next_occ,         # future date — dialog shows as "Next occurrence"
            cadence=default_cadence,
            memo=txn.memo or "",
        )

        dialog = ScheduleDialog(
            accounts=self._repo.list_accounts(),
            categories=self._repo.list_categories_flat(),
            seed=seed,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        if values is None:
            return

        try:
            self._repo.create_scheduled_txn(
                account_id=values.account_id,
                payee_name=values.payee_name,
                category_id=values.category_id,
                transfer_to_account_id=values.transfer_to_account_id,
                estimated_amount=values.estimated_amount,
                variable=values.variable,
                memo=values.memo,
                cadence=values.cadence,
                anchor_date=values.anchor_date,
                next_due_date=values.next_due_date,
                end_date=values.end_date,
                auto_post=values.auto_post,
                notes=values.notes,
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not create schedule", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Could not create schedule", str(e))
            return

        self.statusBar().showMessage(
            f"Schedule created · next due {values.next_due_date}", 4000,
        )

    def _on_bulk_edit(self) -> None:
        ids = self._selected_txn_ids()
        if len(ids) < 2:
            self.statusBar().showMessage(
                "Bulk edit needs at least 2 selected transactions.", 4000,
            )
            return
        dialog = BulkEditDialog(
            self._categories,
            len(ids),
            payee_names=self._repo.list_payee_names(),
            parent=self,
        )
        if dialog.exec() != BulkEditDialog.Accepted:
            return
        changes = dialog.values()
        if not changes:
            return

        # ADR-020 category-driven transfers: if the user picked a
        # transfer-kind category, prompt for the destination and convert
        # every selected row into a transfer (plus apply any other ticked
        # fields). Otherwise the existing bulk_update path runs as before.
        new_category_id = changes.get("category_id")
        if (
            new_category_id is not None
            and self._category_kind(new_category_id) == "transfer"
        ):
            # Collect the source accounts to exclude from the destination
            # picker — a transfer to itself is invalid.
            source_accounts: set[int] = set()
            for proxy_idx in self._table.selectionModel().selectedRows():
                source_idx = self._proxy.mapToSource(proxy_idx)
                if not source_idx.isValid():
                    continue
                source_accounts.add(self._model.row_at(source_idx.row()).account_id)
            other_id = self._prompt_destination_account(
                exclude_account_ids=source_accounts,
                title="Bulk transfer",
                message=(
                    "You picked a transfer category. Which account is the "
                    "other side for these transactions?"
                ),
            )
            if other_id is None:
                return

            # ADR-036 bulk path: apply phase-1 field updates (payee /
            # status / memo) first, then run the matcher per row, then
            # let the user resolve via the review dialog, then write
            # decisions atomically via bulk_match_or_create_transfers.
            try:
                self._repo.bulk_update_transactions(
                    ids,
                    category_id=new_category_id,
                    payee_name=changes.get("payee_name", self._repo._UNSET),
                    status=changes.get("status", self._repo._UNSET),
                    memo=changes.get("memo", self._repo._UNSET),
                )
            except Exception as e:
                QMessageBox.critical(
                    self, "Bulk transfer failed",
                    f"The category / field update was not applied:\n\n{e}",
                )
                return

            # Build per-row matcher analyses for the review dialog.
            row_lookup = {}
            for i in range(self._model.rowCount()):
                r = self._model.row_at(i)
                row_lookup[r.id] = r
            other_name = ""
            other_currency = ""
            other_acct = self._repo.get_account_by_id(other_id)
            if other_acct is not None:
                other_name = other_acct.name
                other_currency = other_acct.currency
            analyses: list[BulkRowAnalysis] = []
            matcher_errors: list[str] = []
            for tid in ids:
                row = row_lookup.get(tid)
                if row is None:
                    continue
                src_currency = (
                    self._repo.get_account_currency(row.account_id) or ""
                )
                try:
                    candidates = self._repo.find_transfer_candidates(
                        source_txn_id=tid,
                        other_account_id=other_id,
                    )
                except Exception as e:
                    candidates = []
                    matcher_errors.append(f"#{tid}: {e}")
                # ADR-035 amendment 2026-06-07: pre-fill the partner
                # magnitude from the FX table when the row crosses
                # currencies. None when no rate is on file — the dialog
                # surfaces an editable blank field with the right hint.
                fx_prefill = None
                if (
                    other_currency
                    and src_currency
                    and other_currency != src_currency
                ):
                    converted, _ = self._repo.convert_amount(
                        abs(row.amount),
                        from_ccy=src_currency,
                        to_ccy=other_currency,
                        on_date=row.posted_date,
                    )
                    fx_prefill = converted
                analyses.append(BulkRowAnalysis(
                    source_txn_id=tid,
                    source_account_id=row.account_id,
                    source_account_name=row.account_name,
                    source_amount=row.amount,
                    source_currency=src_currency,
                    source_date=row.posted_date,
                    source_payee=row.payee_name,
                    candidates=candidates,
                    dest_currency=other_currency,
                    fx_prefill_amount=fx_prefill,
                ))

            if matcher_errors:
                # Surface non-fatal matcher errors (e.g. missing FX rate
                # on cross-currency rows) without blocking — those rows
                # default to "Create new" in the review.
                self.statusBar().showMessage(
                    f"Matcher skipped {len(matcher_errors)} row(s) "
                    f"due to errors; will default to Create new.",
                    6000,
                )

            review = BulkTransferReviewDialog(
                analyses=analyses,
                other_account_id=other_id,
                other_account_name=other_name,
                category_id=new_category_id,
                parent=self,
            )
            if review.exec() != QDialog.Accepted:
                # User cancelled — but phase-1 field updates already
                # landed (category etc.). Reload so the register reflects
                # them; rows just don't become transfers.
                self._model.reload()
                self._refresh_sidebar_balances()
                return
            plan = review.values()
            try:
                result = self._repo.bulk_match_or_create_transfers(plan)
            except Exception as e:
                QMessageBox.critical(
                    self, "Bulk transfer failed",
                    f"The transfer pairing was not applied:\n\n{e}",
                )
                return
            self._model.reload()
            self._refresh_sidebar_balances()
            self.statusBar().showMessage(
                f"Linked {result.linked} · created {result.created} "
                f"transfer{'s' if result.linked + result.created != 1 else ''}",
                4000,
            )
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
        # Bulk edit (non-transfer path) doesn't move amounts, so skip the
        # sidebar balance refresh.
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
        # Re-run the auto-post sweep against the newly-opened DB. Schedules
        # are per-file, so swapping files means a different set of due
        # auto-posters to materialise (or none, for a fresh DB).
        self._run_auto_post_sweep()

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
            "Bank statements (*.ofx *.qfx *.qif *.csv);;All files (*)",
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
            pending_map = self._service.get_pending_map(token)
            if pending_map is None:
                # Token expired between stage and dialog construction — should
                # not happen but degrade gracefully.
                return
            if not pending_map.headers:
                QMessageBox.warning(
                    self, "Cannot map this file",
                    "The CSV file appears to have no header row, so MFL can't "
                    "offer column names to map. Please add headers to the "
                    "first row and try again.",
                )
                self._service.discard_pending_map(token)
                return
            dialog = CsvMappingDialog(pending_map, parent=self)
            if dialog.exec() != QDialog.Accepted or dialog.mapping is None:
                self._service.discard_pending_map(token)
                return
            try:
                token = self._service.apply_mapping_and_stage(
                    token, dialog.mapping,
                )
            except Exception as e:
                QMessageBox.critical(
                    self, "Import failed",
                    f"Could not apply the column mapping:\n\n{e}",
                )
                return

        # Known format (or just-mapped generic CSV) — commit directly with
        # suggested status and auto-accept all potential matches. Per ADR-010 §6,
        # the no-dialog-for-known-imports feedback, and ADR-021's commit path:
        # once the format is understood, nothing to ask the user; just do it.
        self._commit_pending(token)

    def _commit_pending(self, token: str) -> None:
        """Commit a staged PendingImport silently and show the result in the
        status bar. Shared by the known-format and just-mapped paths in
        _on_import."""
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
            f"Imported {result.imported} new into {pending.account_name} · "
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
                CategoryTypeaheadDelegate(
                    self._repo,
                    self._on_create_category_inline,
                    self._table,
                ),
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

        Reports section is reloaded too — saved reports may have come
        and gone since the last refresh (creates from a report window,
        deletes from the sidebar context menu).
        """
        accounts = self._repo.list_accounts()
        folders = self._repo.list_folders()
        balances = self._repo.compute_account_values()
        reports = self._repo.list_reports()
        report_folders = self._repo.list_report_folders()
        self._sidebar.reload(
            accounts, folders, balances,
            reports=reports, report_folders=report_folders,
        )
        if select_iri is not None:
            self._select_account_in_sidebar(select_iri)
        elif accounts:
            self._select_account_in_sidebar(accounts[0].iri)
        else:
            self._sidebar.select_all_transactions()
            self._show_all_transactions()

    def _refresh_sidebar_keep_selection(self) -> None:
        """Reload the sidebar (reports included) preserving whatever the
        user has selected. Used after a saved-report create / update /
        delete so the Reports section reflects the new state without
        moving the user's focus."""
        accounts = self._repo.list_accounts()
        folders = self._repo.list_folders()
        balances = self._repo.compute_account_values()
        reports = self._repo.list_reports()
        report_folders = self._repo.list_report_folders()
        self._sidebar.reload(
            accounts, folders, balances,
            reports=reports, report_folders=report_folders,
        )

    def _on_sidebar_context_menu(self, pos) -> None:
        item = self._sidebar.itemAt(pos)
        if item is None:
            # Empty area — pick the section by y-coordinate so right-
            # clicking *anywhere* in the Reports section (including below
            # the existing rows) offers the Reports verbs. Falls back to
            # the Accounts menu when above both sections.
            section = self._sidebar.section_at_y(pos.y())
            menu = QMenu(self._sidebar)
            if section == "reports":
                new_report_act = menu.addAction("&New Report…")
                new_report_act.triggered.connect(self._on_new_report_from_sidebar)
                new_folder_act = menu.addAction("New &Folder…")
                new_folder_act.triggered.connect(self._on_new_report_folder)
            else:
                menu.addAction(self._new_account_action)
                menu.addAction(self._new_folder_action)
            menu.exec(self._sidebar.viewport().mapToGlobal(pos))
            return
        kind = item.data(0, KIND_ROLE)
        if kind == "section_accounts":
            menu = QMenu(self._sidebar)
            menu.addAction(self._new_account_action)
            menu.addAction(self._new_folder_action)
            menu.exec(self._sidebar.viewport().mapToGlobal(pos))
            return
        if kind == "section_reports":
            menu = QMenu(self._sidebar)
            new_report_act = menu.addAction("&New Report…")
            new_report_act.triggered.connect(self._on_new_report_from_sidebar)
            new_folder_act = menu.addAction("New &Folder…")
            new_folder_act.triggered.connect(self._on_new_report_folder)
            menu.exec(self._sidebar.viewport().mapToGlobal(pos))
            return
        if kind == "report_folder":
            folder_id = item.data(0, Qt.UserRole)
            folder_name = item.text(0)
            menu = QMenu(self._sidebar)
            rename_act = menu.addAction("&Rename Folder…")
            rename_act.triggered.connect(
                lambda: self._on_rename_report_folder(folder_id, folder_name)
            )
            menu.addSeparator()
            up_act = menu.addAction("Move &Up")
            up_act.triggered.connect(
                lambda: self._on_move_report_folder(folder_id, -1)
            )
            down_act = menu.addAction("Move &Down")
            down_act.triggered.connect(
                lambda: self._on_move_report_folder(folder_id, +1)
            )
            menu.addSeparator()
            new_report_act = menu.addAction("&New Report…")
            new_report_act.triggered.connect(self._on_new_report_from_sidebar)
            new_folder_act = menu.addAction("New &Folder…")
            new_folder_act.triggered.connect(self._on_new_report_folder)
            menu.addSeparator()
            delete_act = menu.addAction("&Delete Folder…")
            delete_act.triggered.connect(
                lambda: self._on_delete_report_folder(folder_id, folder_name)
            )
            menu.exec(self._sidebar.viewport().mapToGlobal(pos))
            return
        if kind == "report":
            report_id = int(item.data(0, Qt.UserRole))
            report_name = item.text(0)
            menu = QMenu(self._sidebar)
            open_act = menu.addAction("&Open Report")
            open_act.triggered.connect(
                lambda: self._open_saved_report(report_id)
            )
            menu.addSeparator()
            move_menu = menu.addMenu("&Move to Folder")
            self._populate_move_report_to_folder_menu(move_menu, report_id)
            menu.addSeparator()
            delete_act = menu.addAction("&Delete Report…")
            delete_act.triggered.connect(
                lambda: self._on_delete_report(report_id, report_name)
            )
            menu.exec(self._sidebar.viewport().mapToGlobal(pos))
            return
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
            # Summary first — it's the verb a Banktivity user reaches for
            # most often from a sidebar right-click (ADR-033).
            menu.addAction(self._account_summary_action)
            menu.addSeparator()
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

    def _on_manage_schedules(self) -> None:
        dialog = SchedulesDialog(self._repo, parent=self)
        # Post Now materialises a real txn (or transfer pair) that should
        # appear in the register immediately, and may move balances —
        # refresh the model + sidebar after any successful mutation.
        dialog.schedules_changed.connect(self._on_schedules_changed)
        dialog.exec()

    def _on_manage_currencies(self) -> None:
        """Open Manage → Currencies… (ADR-035). The dialog persists its
        own edits to ``setting`` and ``fx_rate``; we just hand it the
        Repository and let it run."""
        dialog = CurrenciesDialog(self._repo, parent=self)
        dialog.exec()

    def _on_manage_securities(self) -> None:
        """Open Manage → Securities… (ADR-044). The dialog persists its own
        edits to ``setting`` and ``security_price``; refresh sidebar balances
        afterward in case prices changed an investment account's market value."""
        dialog = SecuritiesDialog(self._repo, parent=self)
        dialog.exec()
        self._refresh_sidebar_balances()

    def _on_reconcile_transfers(self) -> None:
        """Open Manage → Reconcile Transfers… (ADR-037).

        The dialog writes through ``Repository.bulk_match_or_create_transfers``
        on Apply. We refresh the model + sidebar after the dialog closes
        in case any pairs were linked (delete-partner-aware queries
        already use transfer_id so the register reflects the change
        without further intervention)."""
        dialog = TransferReconcileDialog(self._repo, parent=self)
        dialog.exec()
        self._model.reload()
        self._refresh_sidebar_balances()

    def _on_schedules_changed(self) -> None:
        self._model.reload()
        self._refresh_sidebar_balances()

    def _run_auto_post_sweep(self) -> None:
        """Materialise any auto-post schedules whose next-due date has
        already arrived. Idempotent: each post advances next_due_date
        past today, so re-running on the same launch posts nothing.
        Failures inside individual schedules are swallowed by the
        repository sweep (see ``auto_post_due``); we surface the count
        only if it was non-zero so a quiet startup stays quiet."""
        try:
            posted = self._repo.auto_post_due(date.today().isoformat())
        except Exception:
            # The whole sweep failed (unexpected DB error). Don't refuse
            # to launch over it — the user can still use the app and
            # see the schedules via the dialog.
            return
        if not posted:
            return
        self._model.reload()
        self._refresh_sidebar_balances()
        self.statusBar().showMessage(
            f"Auto-posted {len(posted)} scheduled "
            f"transaction{'s' if len(posted) != 1 else ''}.", 8000,
        )

    # ── reports ──

    def _on_spending_report(self) -> None:
        """Reports menu → Spending Over Time. Opens the *bare* window
        (no saved-state attached) — saved reports open via the sidebar
        instead (ADR-039 §reports-menu)."""
        self._open_bare_report(TYPE_SPENDING_OVER_TIME)

    def _open_bare_report(self, type_key: str) -> None:
        """Open an unattached report window for the given type. Singleton
        per type — repeat menu clicks raise the existing bare window."""
        existing = self._bare_report_wins.get(type_key)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        if type_key == TYPE_SPENDING_OVER_TIME:
            win = SpendingReportWindow.open_bare(self._repo, parent=self)
        else:
            # Other types not yet implemented; the menu items for them
            # haven't been added either, but keep the dispatcher honest.
            QMessageBox.information(
                self, "Not yet available",
                "This report type is part of a later round of ADR-039.",
            )
            return
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(
            lambda _obj=None, t=type_key: self._on_bare_report_closed(t)
        )
        win.reports_changed.connect(self._on_reports_changed)
        self._bare_report_wins[type_key] = win
        win.show()

    def _on_bare_report_closed(self, type_key: str) -> None:
        self._bare_report_wins.pop(type_key, None)

    def _open_saved_report(self, report_id: int) -> None:
        """Open a saved report by id. Singleton per report id — clicking
        the same Reports-section row twice raises the existing window."""
        existing = self._saved_report_wins.get(report_id)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        report = self._repo.get_report(report_id)
        if report is None:
            # Row was deleted between sidebar build and click.
            self._refresh_sidebar_keep_selection()
            return
        if report.type == TYPE_SPENDING_OVER_TIME:
            win = SpendingReportWindow.load_from_id(
                self._repo, report_id, parent=self,
            )
        else:
            QMessageBox.information(
                self, "Not yet available",
                f"Saved reports of type {report.type!r} aren't openable yet.",
            )
            return
        if win is None:
            self._refresh_sidebar_keep_selection()
            return
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(
            lambda _obj=None, rid=report_id: self._on_saved_report_closed(rid)
        )
        win.reports_changed.connect(self._on_reports_changed)
        self._saved_report_wins[report_id] = win
        win.show()

    def _on_saved_report_closed(self, report_id: int) -> None:
        self._saved_report_wins.pop(report_id, None)

    def _on_reports_changed(self) -> None:
        """A report window saved / created / renamed a row. Refresh the
        sidebar's Reports section so the new state is visible without
        the user having to do anything."""
        self._refresh_sidebar_keep_selection()

    # ── reports — sidebar verbs ──

    def _on_new_report_from_sidebar(self) -> None:
        """Sidebar → New Report… verb. Pops the type picker; opens the
        bare window for the chosen type. The user's first Save inside
        that window creates the actual ``report`` row."""
        dialog = NewReportDialog(parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        type_key = dialog.values()
        if type_key is None:
            return
        self._open_bare_report(type_key)

    def _on_new_report_folder(self) -> None:
        name, ok = QInputDialog.getText(
            self, "New Folder", "Folder name:", QLineEdit.Normal, "",
        )
        if not ok or not name.strip():
            return
        try:
            self._repo.create_report_folder(name)
        except ValueError as e:
            QMessageBox.warning(self, "Could not create folder", str(e))
            return
        self._refresh_sidebar_keep_selection()

    def _on_rename_report_folder(self, folder_id: int, current_name: str) -> None:
        new_name, ok = QInputDialog.getText(
            self, "Rename Folder", "New name:", QLineEdit.Normal, current_name,
        )
        if not ok or new_name.strip() == current_name:
            return
        try:
            self._repo.rename_report_folder(folder_id, new_name)
        except ValueError as e:
            QMessageBox.warning(self, "Could not rename", str(e))
            return
        self._refresh_sidebar_keep_selection()

    def _on_delete_report_folder(self, folder_id: int, folder_name: str) -> None:
        confirm = QMessageBox.question(
            self, "Confirm delete",
            f"Delete folder {folder_name!r}?\n\nReports inside the folder "
            f"will move back to the Reports root — no reports are deleted.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._repo.delete_report_folder(folder_id)
        except Exception as e:
            QMessageBox.critical(self, "Could not delete folder", str(e))
            return
        self._refresh_sidebar_keep_selection()

    def _on_move_report_folder(self, folder_id: int, direction: int) -> None:
        try:
            self._repo.move_report_folder(folder_id, direction)
        except Exception as e:
            QMessageBox.critical(self, "Could not reorder folder", str(e))
            return
        self._refresh_sidebar_keep_selection()

    def _on_delete_report(self, report_id: int, report_name: str) -> None:
        confirm = QMessageBox.question(
            self, "Confirm delete",
            f"Delete saved report {report_name!r}?\n\n"
            f"This removes the saved filter set; transaction data is "
            f"unaffected.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._repo.delete_report(report_id)
        except Exception as e:
            QMessageBox.critical(self, "Could not delete report", str(e))
            return
        # Close the open window for this report (if any) so the user
        # doesn't end up editing a phantom row.
        existing = self._saved_report_wins.pop(report_id, None)
        if existing is not None:
            existing.close()
        self._refresh_sidebar_keep_selection()

    def _populate_move_report_to_folder_menu(
        self, menu: "QMenu", report_id: int,
    ) -> None:
        """Build the Move to Folder ▸ submenu for a right-clicked report.
        Mirrors :py:meth:`_populate_move_to_folder_menu` (account
        equivalent)."""
        folders = self._repo.list_report_folders()
        report = self._repo.get_report(report_id)
        current_folder_id = report.folder_id if report is not None else None

        no_folder_act = menu.addAction("(No folder)")
        no_folder_act.setEnabled(current_folder_id is not None)
        no_folder_act.triggered.connect(
            lambda: self._move_report_to_folder(report_id, None)
        )

        if folders:
            menu.addSeparator()
            for f in folders:
                act = menu.addAction(f.name)
                act.setEnabled(f.id != current_folder_id)
                fid = f.id
                act.triggered.connect(
                    lambda checked=False, target=fid: self._move_report_to_folder(
                        report_id, target,
                    )
                )

        menu.addSeparator()
        new_act = menu.addAction("&New Folder…")
        new_act.triggered.connect(
            lambda: self._on_new_report_folder_and_assign(report_id)
        )

    def _move_report_to_folder(
        self, report_id: int, folder_id: Optional[int],
    ) -> None:
        try:
            self._repo.set_report_folder(report_id, folder_id)
        except ValueError as e:
            QMessageBox.warning(self, "Could not move report", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Could not move report", str(e))
            return
        self._refresh_sidebar_keep_selection()

    def _on_new_report_folder_and_assign(self, report_id: int) -> None:
        name, ok = QInputDialog.getText(
            self, "New Folder", "Folder name:", QLineEdit.Normal, "",
        )
        if not ok or not name.strip():
            return
        try:
            folder = self._repo.create_report_folder(name)
            self._repo.set_report_folder(report_id, folder.id)
        except ValueError as e:
            QMessageBox.warning(self, "Could not create folder", str(e))
            return
        self._refresh_sidebar_keep_selection()

    def _on_net_worth(self) -> None:
        existing = getattr(self, "_net_worth_win", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        win = NetWorthWindow(self._repo, parent=self)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(self._on_net_worth_closed)
        self._net_worth_win = win
        win.show()

    def _on_net_worth_closed(self, _obj=None) -> None:
        self._net_worth_win = None

    def _on_open_budget(self) -> None:
        """Open the Budget window — non-modal, singleton like the other
        report windows. The window auto-refreshes on activation so flipping
        back from the register reflects new actuals (ADR-024)."""
        existing = getattr(self, "_budget_win", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        win = BudgetWindow(self._repo, parent=self)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(self._on_budget_closed)
        self._budget_win = win
        win.show()

    def _on_budget_closed(self, _obj=None) -> None:
        self._budget_win = None

    # ── account summary (ADR-033) ──

    def _on_sidebar_double_click(self, item, _column) -> None:
        """Double-click on an account row opens its Account Summary
        screen. Folders + 'All transactions' fall through (folders already
        toggle expansion via their single-click handler)."""
        if item is None or item.data(0, KIND_ROLE) != "account":
            return
        iri = item.data(0, Qt.UserRole)
        if iri is None:
            return
        acct = self._repo.get_account_by_iri(iri)
        if acct is None:
            return
        self._open_account_summary(acct.id)

    def _on_open_account_summary_for_selection(self) -> None:
        """Account → Summary…/Ctrl+I handler. Opens the summary for the
        currently-selected account; no-op when the All-transactions view
        is showing (the action is disabled in that state but the shortcut
        could still fire via window focus)."""
        if self._account is None:
            return
        self._open_account_summary(self._account.id)

    def _open_account_summary(self, account_id: int) -> None:
        existing = self._account_summary_wins.get(account_id)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        win = AccountSummaryWindow(self._repo, account_id, parent=self)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(
            lambda _obj=None, aid=account_id: self._on_account_summary_closed(aid)
        )
        self._account_summary_wins[account_id] = win
        win.show()

    def _on_account_summary_closed(self, account_id: int) -> None:
        self._account_summary_wins.pop(account_id, None)

    # ── reconciliation (ADR-040) ──

    def _on_reconcile_for_selection(self) -> None:
        """Account → Reconcile… / Ctrl+Alt+R / register Reconcile button.
        Opens the statement history for the selected account; no-op in the
        all-transactions view (the action/button are disabled there, but the
        shortcut could still fire via window focus)."""
        if self._account is None:
            return
        dialog = StatementsWindow(self._repo, self._account, parent=self)
        dialog.statements_changed.connect(self._on_statements_changed)
        dialog.exec()

    def _on_statements_changed(self) -> None:
        """A close / reopen / delete changed txn statuses — refresh the
        register and sidebar so Reconciled rows and balances are current."""
        self._model.reload()
        self._refresh_sidebar_balances()

    def _confirm_reconciled_edit(self, txn_id: int) -> bool:
        """Model gate: warn before an inline edit lands on a reconciled row.
        Returns True to allow the edit, False to reject it."""
        stmt = self._repo.get_statement_for_txn(txn_id)
        when = ""
        if stmt is not None:
            try:
                d = date.fromisoformat(stmt.end_date)
                when = f" dated {d.day} {d.strftime('%b %Y')}"
            except ValueError:
                when = ""
        resp = QMessageBox.question(
            self, "Reconciled transaction",
            f"This transaction is reconciled to a statement{when}.\n\n"
            "Changing it may put that statement out of balance. "
            "Change anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return resp == QMessageBox.Yes

    def _populate_category_combo(self) -> None:
        """Rebuild the filter-bar Category combo, scoped to the
        categories actually in use in the current view (single account
        or all accounts). Preserves the previously-selected filter id
        where it's still visible; otherwise reverts to All and clears
        the proxy filter in lockstep."""
        current_id = (
            self._category_combo.currentData()
            if self._category_combo.count() else None
        )
        in_use = self._repo.distinct_category_ids_for_account(
            self._account.id if self._account is not None else None
        )
        self._category_combo.blockSignals(True)
        self._category_combo.clear()
        self._category_combo.addItem("All", userData=None)
        restore_index = 0
        for i, c in enumerate(
            (c for c in self._categories if c.id in in_use), start=1,
        ):
            label = f"{c.name} ({c.parent_name})" if c.parent_name else c.name
            self._category_combo.addItem(label, userData=c.id)
            if c.id == current_id:
                restore_index = i
        self._category_combo.setCurrentIndex(restore_index)
        self._category_combo.blockSignals(False)
        # blockSignals(True) above swallows currentIndexChanged, so if
        # the prior selection isn't visible in the new view we have to
        # push the "All" filter through to the proxy by hand — otherwise
        # the table keeps showing the now-invisible category's rows only.
        if current_id is not None and restore_index == 0:
            self._proxy.set_category_id(None)
