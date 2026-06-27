"""Register main window — top-level Qt surface for the desktop app.

Multi-account: a left-hand sidebar lists accounts with "All transactions"
on top. Selecting an account swaps the model and column layout; selecting
"All transactions" shows the cross-account aggregate (Account column added,
Balance column hidden — see project-all-transactions-view in memory).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QEvent, QStandardPaths, QUrl
from PySide6.QtGui import QAction, QCursor, QKeySequence, QDesktopServices
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
    QStackedWidget,
    QStatusBar,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop import data_library, snapshots
from mfl_desktop import fx, prices
from mfl_desktop import license_service
from mfl_desktop import version
from mfl_desktop.app_session import remember_last_db, set_snapshots_root
from mfl_desktop.licensing import STATE_EXPIRED, STATE_TRIAL
from mfl_desktop.account_summary import bills_due_summary
from mfl_desktop import periods
from mfl_desktop.db.repository import AccountSummary, Repository
from mfl_desktop.import_engine.import_service import ImportService
from mfl_desktop.import_engine.qif_actions import is_categorisable
from mfl_desktop.ui.account_dialog import AccountDialog
from mfl_desktop.ui.account_summary_window import AccountSummaryWindow
from mfl_desktop.ui.budget_window import BudgetWindow
from mfl_desktop.ui.bulk_edit_dialog import BulkEditDialog
from mfl_desktop.ui.categories_dialog import CategoriesDialog
from mfl_desktop.ui.csv_mapping_dialog import CsvMappingDialog
from mfl_desktop.ui.import_review_dialog import ImportReviewDialog
from mfl_desktop.ui.currencies_dialog import CurrenciesDialog
from mfl_desktop.ui.data_library_dialog import DataLibraryDialog
from mfl_desktop.ui.home_view import HomeView
from mfl_desktop.ui import tokens
from mfl_desktop.ui.theme import apply_theme, SETTING_KEY as THEME_SETTING_KEY
from mfl_desktop.ui.bank_feeds_dialog import BankFeedsDialog
from mfl_desktop.ui.securities_dialog import SecuritiesDialog
from mfl_desktop.ui.transfer_reconcile_dialog import TransferReconcileDialog
from mfl_desktop.ui.delegates import (
    CategoryTypeaheadDelegate,
    DateEditDelegate,
    PayeeTypeaheadDelegate,
    StatusDelegate,
)
from mfl_desktop.ui.filter_proxy import TransactionFilterProxy
from mfl_desktop.ui.register_filters_popover import RegisterFiltersPopover
from mfl_desktop.ui.payees_dialog import PayeesDialog
from mfl_desktop.ui.memorise_category_dialog import MemoriseCategoryDialog
from mfl_desktop.ui.rules_dialog import RulesDialog
from mfl_desktop.ui.register_model import TransactionTableModel
from mfl_desktop.ui.schedule_dialog import ScheduleDialog, ScheduleSeed
from mfl_desktop.ui.schedules_dialog import SchedulesDialog
from mfl_desktop.ui.sidebar import CLOSED_ROLE, KIND_ROLE, Sidebar
from mfl_desktop.ui.statements_window import StatementsWindow
from mfl_desktop.ui.net_worth_window import NetWorthWindow
from mfl_desktop.ui.new_report_dialog import NewReportDialog
from mfl_desktop.ui.spending_report_window import SpendingReportWindow
from mfl_desktop.ui.income_report_window import IncomeReportWindow
from mfl_desktop.ui.income_expense_window import IncomeExpenseWindow
from mfl_desktop.ui.investment_returns_window import InvestmentReturnsWindow
from mfl_desktop.ui.investment_income_window import InvestmentIncomeWindow
from mfl_desktop.ui.sankey_report_window import SankeyReportWindow
from mfl_desktop.ui.payee_report_window import PayeeReportWindow
from mfl_desktop.ui.category_payee_window import CategoryPayeeWindow
from mfl_desktop.reports.filters import (
    TYPE_CATEGORY_PAYEE,
    TYPE_INCOME_EXPENSE,
    TYPE_INCOME_OVER_TIME,
    TYPE_INVESTMENT_RETURNS,
    TYPE_NET_WORTH,
    TYPE_PAYEE,
    TYPE_SANKEY,
    TYPE_SPENDING_OVER_TIME,
)
from mfl_desktop.ui.transaction_dialog import NewTransactionDialog
from mfl_desktop.ui.investment_transaction_dialog import InvestmentTransactionDialog
from mfl_desktop.ui.split_transaction_dialog import SplitTransactionDialog
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
# ADR-041 (amended 2026-06-14): the original 30d/90d/ytd/all set felt too
# restrictive for everyday scanning, so 6- and 12-month windows were added and
# the default moved to 12 months. The default is still a *windowed* query, so
# even a big account opens cheaply (and ADR-061 made in-view search fast).
# Register date-window presets (ADR-041) — now sourced from the shared period
# vocabulary (ADR-082). `_months_before` moved to periods.months_before.
_WINDOW_PRESETS = periods.options_for(periods.REGISTER_PRESETS)
_DEFAULT_WINDOW_KEY = periods.DEFAULT_REGISTER_KEY

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
    # ADR-062: Filters button label, with a trailing dot when any date/amount
    # filter is active.
    _FILTERS_LABEL = "Filters ▾"
    _FILTERS_LABEL_ACTIVE = "Filters ▾  ●"
    # A6 (ADR-063): base label for the register's Schedules button. The
    # overdue/due-soon cue decorates it with a coloured count in
    # _refresh_schedules_cue(); this is the plain "nothing due" form.
    _SCHEDULES_LABEL = "Schedules"

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
        # Name of the library dataset loaded onto the working file, if any
        # (ADR-059) — shown in the title in place of the bench filename.
        self._loaded_dataset: Optional[str] = None
        # ADR-041: current register date-window key; resolved to a posted_date
        # lower bound by _current_since(). Set before the initial selection
        # builds a model so the first view is already windowed.
        self._window_key = _DEFAULT_WINDOW_KEY

        self.resize(1360, 760)

        # ── sidebar + filter bar ──

        # include_closed=True so the sidebar can render the 'Closed accounts'
        # group (ADR-069); it partitions open vs closed itself.
        accounts = repo.list_accounts(include_closed=True)
        folders = repo.list_folders()
        # Sidebar shows each account's worth — market value for investment
        # accounts (cash + holdings), cash for everything else (ADR-044).
        balances = repo.compute_account_values(include_closed=True)
        reports = repo.list_reports()
        report_folders = repo.list_report_folders()
        self._sidebar = Sidebar(
            accounts, folders, balances,
            reports=reports, report_folders=report_folders,
            repo=repo,
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
        # Value type is the per-type report window (SpendingReportWindow or
        # InvestmentReturnsWindow today — both QMainWindow with the same
        # reports_changed signal + WA_DeleteOnClose contract).
        self._saved_report_wins: dict[int, QMainWindow] = {}
        self._bare_report_wins: dict[str, QMainWindow] = {}
        # ADR-108: the Investment Income view is a live analysis window (no
        # saved type), kept singleton via this reference rather than the
        # report-framework registries above.
        self._investment_income_win: Optional[QMainWindow] = None

        search = QLineEdit()
        search.setPlaceholderText("Search payee, memo, amount, or date…")
        # ADR-061: debounce. Applying the filter re-evaluates every loaded row,
        # and on a wide date window ("Show: All") that is tens of thousands —
        # so filtering on each keystroke froze the UI while typing. Collapse a
        # burst of keystrokes into a single filter pass ~200 ms after typing
        # stops.
        self._search_pending = ""
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(
            lambda: self._proxy.set_search(self._search_pending)
        )
        search.textChanged.connect(self._on_search_text_changed)

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
        # "Unreconciled" is a meta-status (everything except Reconciled), handy
        # for working down the rows that still need clearing/reconciling.
        status_combo.addItems(["All", "Unreconciled", *STATUSES])
        status_combo.currentTextChanged.connect(lambda s: self._proxy.set_status(s))

        # A2 (2026-06-14): the Category filter combo was removed — the general
        # Search box now matches category names too (see register_model's
        # _build_search_blob), so typing e.g. "groceries" filters the register
        # without a dedicated combo. The proxy keeps its set_category_id()
        # capability for other callers; it's simply no longer driven here.

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
        # A5 (ADR-062): date-range + amount-range filters live in a popover so
        # the bar stays uncluttered. _filter_from_iso (the date-From lower bound)
        # also widens the load window via _effective_since so an early From is
        # reachable regardless of the Show preset.
        self._filter_from_iso: Optional[str] = None
        self._filters_popover: Optional[RegisterFiltersPopover] = None
        self._filters_btn = QPushButton(self._FILTERS_LABEL)
        self._filters_btn.clicked.connect(self._on_filters_button)
        filter_bar.addWidget(self._filters_btn)
        filter_bar.addStretch(1)
        # A3 (2026-06-14): a visible New Transaction button for mouse/trackpad
        # users — same handler as the Transaction menu item and Ctrl/⌘+N.
        self._new_txn_btn = QPushButton("＋ New Transaction")
        self._new_txn_btn.clicked.connect(self._on_new_transaction)
        filter_bar.addWidget(self._new_txn_btn)
        filter_bar.addSpacing(12)
        # A6 (ADR-063): direct access to the cross-account Schedules dialog
        # from the register — the same dialog as Manage ▸ Schedules — plus an
        # overdue/due-soon cue so bills don't slip past unnoticed. The label
        # gains a coloured count via _refresh_schedules_cue() (called at the
        # end of __init__ after the auto-post sweep, on schedule changes, and
        # on window re-activation).
        self._schedules_btn = QPushButton(self._SCHEDULES_LABEL)
        self._schedules_btn.clicked.connect(self._on_manage_schedules)
        filter_bar.addWidget(self._schedules_btn)
        filter_bar.addSpacing(12)
        # Reconcile button — opens the statement history for the current
        # account (ADR-040). Disabled in all-transactions mode, in lockstep
        # with the Account → Reconcile… menu action.
        self._reconcile_btn = QPushButton("Reconcile…")
        self._reconcile_btn.clicked.connect(self._on_reconcile_for_selection)
        filter_bar.addWidget(self._reconcile_btn)

        # ── table ──

        self._table = QTableView()
        # macOS focus ring around the grid — remove it (ADR-076); QSS
        # `outline: 0` doesn't cover the native ring. Harmless elsewhere.
        self._table.setAttribute(Qt.WA_MacShowFocusRect, False)
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
        # ADR-048: double-clicking an investment row opens the edit dialog
        # (investment cells are read-only inline, so this is the edit path).
        self._table.doubleClicked.connect(self._on_table_double_clicked)

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

        # ADR-075: the right side is a stack — page 0 the Home dashboard,
        # page 1 the register/report panel. Selecting Home shows the dashboard;
        # selecting an account / All transactions flips to the register.
        self._home_view = HomeView(repo, self)
        self._home_view.net_worth_requested.connect(self._on_net_worth)
        self._home_view.budget_requested.connect(self._on_open_budget)
        self._home_view.schedules_requested.connect(self._on_manage_schedules)
        self._home_view.payee_report_requested.connect(self._on_payee_report)
        self._home_view.spending_report_requested.connect(self._on_spending_report)
        self._home_view.account_requested.connect(self._select_account_in_sidebar)

        self._main_stack = QStackedWidget()
        self._main_stack.addWidget(self._home_view)    # index 0
        self._main_stack.addWidget(right_panel)        # index 1

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._sidebar)
        splitter.addWidget(self._main_stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 1000])
        self.setCentralWidget(splitter)

        self.setStatusBar(QStatusBar(self))
        self._build_menus()
        self._build_toolbar()

        # ADR-061: coalesce status-bar refreshes. A single filter invalidation
        # emits rowsRemoved + rowsInserted + layoutChanged, and _update_status
        # walks every visible row (mapToSource + Decimal sum for the net), so
        # firing it per signal meant 2–3 full walks per keystroke. Route the
        # signals through a zero-delay single-shot timer that restarts on each
        # emission, collapsing a burst into one walk per event-loop turn.
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(0)
        self._status_timer.timeout.connect(self._update_status)
        self._proxy.layoutChanged.connect(self._schedule_status_update)
        self._proxy.modelReset.connect(self._schedule_status_update)
        self._proxy.rowsInserted.connect(self._schedule_status_update)
        self._proxy.rowsRemoved.connect(self._schedule_status_update)

        # ── initial selection ──

        if initial_account_iri is not None:
            self._select_account_in_sidebar(initial_account_iri)
        else:
            # ADR-075: Home is the default landing view on launch.
            self._sidebar.select_home()
            self._show_home()

        # Auto-post any schedules that have come due since the last launch —
        # see ADR-023. Runs against the freshly-opened DB only; the user's
        # variable-amount and manual schedules are left for them to action
        # via Manage ▸ Schedules.
        self._run_auto_post_sweep()

        # A6 (ADR-063): paint the Schedules cue once the launch sweep has
        # advanced any auto-posters past today, so an auto-paid bill the
        # sweep just handled doesn't briefly flash as overdue.
        self._refresh_schedules_cue()

        # Automatic rotating backups (ADR-057). Snapshot now — capturing the
        # state we opened against, *before* this session's edits, so the user
        # has a clean rollback point — then every interval_min minutes while the
        # app is open, plus a final one on clean close (closeEvent). Retention
        # (the GFS policy, ADR-060) keeps a thinning set in a Snapshots/ folder
        # beside the live database. All best-effort: a backup never blocks the UI.
        snapshots.maybe_snapshot(self._repo)
        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.timeout.connect(self._take_snapshot)
        self._apply_snapshot_interval()
        self._snapshot_timer.start()

        # ADR-079: gentle licensing surface — title-bar cue always, plus a
        # one-off launch prompt when the trial is ending/ended.
        self._license_title_suffix = ""
        self._maybe_show_license_nag()

    def _apply_snapshot_interval(self) -> None:
        """Set the in-session capture cadence from the live file's stored policy
        (ADR-060). Called on launch and after the user edits snapshot settings."""
        policy = snapshots.load_policy(self._repo)
        self._snapshot_timer.setInterval(policy.interval_min * 60 * 1000)

    def _take_snapshot(self) -> None:
        """Periodic in-session backup (ADR-057). Reads ``self._repo`` fresh so
        it follows the live file across a File ▸ Open swap. Silent when nothing
        has changed since the last snapshot."""
        path = snapshots.maybe_snapshot(self._repo)
        if path is not None:
            self.statusBar().showMessage(f"Backed up to {path.name}", 3000)

    def closeEvent(self, event) -> None:
        """Clean shutdown (ADR-057): take a final backup, fold the WAL into the
        main file so the single ``.mfl`` is self-contained, then close the
        connection. Every step is best-effort — a backup or checkpoint failure
        must never trap the user in the app."""
        self._snapshot_timer.stop()
        self._flush_and_close()
        super().closeEvent(event)

    def on_about_to_quit(self) -> None:
        """Safety net for a quit that doesn't route through ``closeEvent`` —
        e.g. Cmd/Ctrl-Q or app-level quit (ADR-109). Idempotent: ``_flush_and_close``
        no-ops once the repo is closed, so running both here and in ``closeEvent``
        is harmless. Guarantees the WAL is folded into the ``.mfl`` (the
        'auto-save on exit' the owner asked for) however the app exits."""
        self._snapshot_timer.stop()
        self._flush_and_close()

    def _flush_and_close(self) -> None:
        """Final backup → WAL checkpoint → close, guarded so it runs at most
        once. Best-effort throughout."""
        if not self._repo.is_open():
            return
        snapshots.maybe_snapshot(self._repo)
        self._repo.checkpoint()
        self._repo.close()

    def changeEvent(self, event) -> None:
        """Refresh the Schedules cue when the window regains focus (A6,
        ADR-063). The overdue/due-soon split is date-relative, so an app
        left open across midnight — or a schedule posted from another
        window — should re-colour the button without forcing a relaunch.
        Cheap enough to run on every activation (a handful of schedules)."""
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            self._refresh_schedules_cue()
            # ADR-075: keep the Home dashboard fresh when it's the visible page
            # and the window regains focus (edits made elsewhere show up).
            # getattr-guarded: an ActivationChange can fire during construction
            # before the stack exists.
            stack = getattr(self, "_main_stack", None)
            if stack is not None and stack.currentIndex() == 0:
                self._home_view.refresh()

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
        if kind == "home":
            self._show_home()
            return
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

    def _show_home(self) -> None:
        """Show the Home dashboard page (ADR-075) and refresh it from the DB."""
        self._home_view.refresh()
        self._main_stack.setCurrentIndex(0)
        self._account = None
        self._update_window_title()

    def _show_account(self, account_iri: str) -> None:
        acct = self._repo.get_account_by_iri(account_iri)
        if acct is None:
            self._show_all_transactions()
            return
        self._main_stack.setCurrentIndex(1)
        self._account = acct
        self._update_window_title()
        self._set_model(TransactionTableModel(
            self._repo, account_id=acct.id, since=self._effective_since(),
            invest=(acct.family == "investment"),
        ))
        self._import_action.setEnabled(True)
        self._import_action.setToolTip("Import OFX / QFX / QIF / CSV into this account")
        self._import_latest_action.setEnabled(True)
        self._set_account_action_state(account_selected=True)

    def refresh_after_first_run(self) -> None:
        """Re-render the surfaces the onboarding dialog can change — the Home
        dashboard (display currency) and the sidebar (account name + balance
        currency label). Cheap; called once after the welcome dialog closes."""
        self._home_view.refresh()
        self._refresh_sidebar_balances()

    def start_first_run_import(self, account_iri: str) -> None:
        """Navigate to ``account_iri`` and open the import picker on it
        (ADR-098 first-run "Import a statement…"). Imports always target a
        specific account, so we select it first; ``_show_account`` enables
        the import action, then we reuse the normal import handler."""
        self._show_account(account_iri)
        if self._account is not None:
            self._on_import()

    def _show_all_transactions(self) -> None:
        self._main_stack.setCurrentIndex(1)
        self._account = None
        self._update_window_title()
        self._set_model(TransactionTableModel(
            self._repo, account_id=None, since=self._effective_since(),
        ))
        self._import_action.setEnabled(False)
        self._import_latest_action.setEnabled(False)
        self._import_action.setToolTip(
            "Select an account in the sidebar to import into it"
        )
        self._set_account_action_state(account_selected=False)

    def _current_since(self) -> Optional[str]:
        """Resolve the active window key (ADR-041) to an inclusive
        'YYYY-MM-DD' lower bound on posted_date, or None for full history.
        Period maths lives in mfl_desktop.periods (ADR-082)."""
        return periods.period_since(self._window_key, date.today())

    def _effective_since(self) -> Optional[str]:
        """The actual load lower bound (ADR-062): the Show preset's `since`,
        widened earlier if the Filters popover's date-From sits before it, so an
        early From is reachable. `None` (All / no preset bound) loads everything,
        so a From can only narrow there (handled by the proxy), never widen."""
        preset = self._current_since()
        if self._filter_from_iso is None or preset is None:
            return preset
        return min(preset, self._filter_from_iso)

    def _on_window_change(self, index: int) -> None:
        """Date-window combo changed — re-window the current model in place.

        The column layout is identical across windows, so set_since just
        resets the rows (no delegate/​column-width teardown). The proxy
        re-sorts on the model reset, keeping the active sort column."""
        self._window_key = self._window_combo.itemData(index)
        self._model.set_since(self._effective_since())

    def _update_window_title(self) -> None:
        # A dataset loaded from the library is a working *copy* on the bench file,
        # so its name (not the bench filename) is what the user thinks they're in.
        loaded = getattr(self, "_loaded_dataset", None)
        filename = loaded if loaded else self._repo.db_path.name
        if self._account is None:
            suffix = "All transactions"
        else:
            suffix = f"{self._account.name}  ·  {self._account.currency}"
        # ADR-079: a quiet, always-visible trial/expired cue in the title bar.
        lic = getattr(self, "_license_title_suffix", "")
        self.setWindowTitle(f"My Financial Life — {filename} — {suffix}{lic}")

    def _set_account_action_state(self, account_selected: bool) -> None:
        """Enable Edit/Delete/Summary only when a specific account is being viewed."""
        if hasattr(self, "_edit_account_action"):
            self._edit_account_action.setEnabled(account_selected)
            self._delete_account_action.setEnabled(account_selected)
        if hasattr(self, "_close_account_action"):
            # Close only applies to an open account; reopening a closed one
            # is offered via the sidebar context menu (ADR-069).
            self._close_account_action.setEnabled(
                account_selected
                and self._account is not None
                and not self._account.is_closed
            )
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
        # Date gets a calendar editor wherever it's editable (cash registers;
        # investment rows are dialog-edited, so their Date column stays read-only).
        date_idx = col_index.get("posted_date")
        if date_idx is not None and self._model.COLUMNS[date_idx][2]:
            self._table.setItemDelegateForColumn(
                date_idx, DateEditDelegate(self._table),
            )
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

    def _on_search_text_changed(self, text: str) -> None:
        # ADR-061: stash the latest text and (re)start the debounce timer; the
        # actual proxy filter runs once typing settles. start() on a running
        # single-shot restarts it, so a fast typist gets one filter pass.
        self._search_pending = text
        self._search_timer.start()

    def _schedule_status_update(self, *args) -> None:
        # ADR-061: coalesce the burst of proxy signals from one filter/sort into
        # a single _update_status walk. *args absorbs the rows{Inserted,Removed}
        # (parent, first, last) payloads.
        self._status_timer.start()

    def _on_filters_button(self) -> None:
        # ADR-062: toggle the date/amount Filters popover under the button.
        if self._filters_popover is None:
            self._filters_popover = RegisterFiltersPopover(self)
            self._filters_popover.filters_changed.connect(self._on_filters_changed)
        pop = self._filters_popover
        if pop.isVisible():
            pop.hide()
        else:
            pop.popup_under(self._filters_btn)

    def _on_filters_changed(
        self, date_from, date_to, amount_min, amount_max,
    ) -> None:
        # ADR-062: push the in-memory date/amount filters; widen the load window
        # first if an early date-From now needs rows the Show preset didn't load.
        self._filter_from_iso = date_from
        new_since = self._effective_since()
        if new_since != self._model.current_since():
            self._model.set_since(new_since)
        self._proxy.set_date_range(date_from, date_to)
        self._proxy.set_amount_range(amount_min, amount_max)
        active = self._filters_popover is not None and self._filters_popover.is_active()
        self._filters_btn.setText(
            self._FILTERS_LABEL_ACTIVE if active else self._FILTERS_LABEL
        )

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

    # ── quick-action toolbar (ADR-116) ──

    def _build_toolbar(self) -> None:
        """A quick-action header for the things that are otherwise buried.

        ``Home`` is only reachable as a sidebar row (ADR-075) and is easy to
        miss; ``Update Prices`` / ``Update Rates`` live three clicks deep inside
        Manage ▸ Securities / Currencies. The toolbar surfaces all three at the
        top of the window, and the two update buttons fetch *directly* (no
        dialog) — the same synchronous, force-refresh path those dialogs' own
        Refresh-Now buttons use.
        """
        tb = QToolBar("Quick actions", self)
        tb.setObjectName("quick_actions_toolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(Qt.TopToolBarArea, tb)
        self._toolbar = tb

        self._toolbar_home_action = QAction("Home", self)
        self._toolbar_home_action.setToolTip("Go to the Home dashboard")
        self._toolbar_home_action.triggered.connect(self._on_go_home)
        tb.addAction(self._toolbar_home_action)

        tb.addSeparator()

        self._update_prices_action = QAction("Update Prices", self)
        self._update_prices_action.setToolTip(
            "Fetch the latest security prices from Tiingo now"
        )
        self._update_prices_action.triggered.connect(self._on_update_prices)
        tb.addAction(self._update_prices_action)

        self._update_rates_action = QAction("Update Rates", self)
        self._update_rates_action.setToolTip(
            "Fetch the latest currency exchange rates from openexchangerates now"
        )
        self._update_rates_action.triggered.connect(self._on_update_rates)
        tb.addAction(self._update_rates_action)

        self._update_all_action = QAction("Update All", self)
        self._update_all_action.setToolTip(
            "Fetch the latest security prices and exchange rates in one go"
        )
        self._update_all_action.triggered.connect(self._on_update_all)
        tb.addAction(self._update_all_action)

    def _on_go_home(self) -> None:
        """Toolbar Home — select Home in the sidebar and show the dashboard
        (mirrors the launch landing path)."""
        self._sidebar.select_home()
        self._show_home()

    _PRICE_ERR_PREAMBLE = (
        "Some prices failed (often a fund ticker Tiingo doesn't cover — price "
        "those manually in Manage ▸ Securities):"
    )
    _RATE_ERR_PREAMBLE = "Some exchange rates failed:"

    def _has_setting(self, key: str) -> bool:
        return bool((self._repo.get_setting(key) or "").strip())

    @staticmethod
    def _count_phrase(n: Optional[int], noun: str) -> str:
        """``3 → "3 prices"``, ``1 → "1 price"``, ``None → "prices: failed"``
        (None means the refresh call raised; its message rides in the errors)."""
        if n is None:
            return f"{noun}s: failed"
        return f"{n} {noun}{'' if n == 1 else 's'}"

    def _warn_refresh_errors(self, title: str, preamble: str, errors) -> None:
        shown = errors[:12]
        more = len(errors) - len(shown)
        body = "\n".join(shown) + (f"\n… and {more} more" if more > 0 else "")
        QMessageBox.warning(self, title, f"{preamble}\n\n{body}")

    def _refresh_prices(self) -> tuple[Optional[int], list]:
        """Force-refresh security prices (Tiingo key assumed set) — the same
        path Manage ▸ Securities ▸ Refresh Now uses. Returns
        ``(count_or_None, errors)``; catches its own exception (→ ``None`` +
        message) so a combined Update All can keep going."""
        try:
            result = prices.refresh_latest_prices_into(self._repo, force=True)
        except Exception as e:  # noqa: BLE001
            return None, [f"Could not update prices: {e}"]
        return result.new_prices_count, result.errors

    def _refresh_rates(self) -> tuple[Optional[int], list]:
        """Force-refresh FX rates (openexchangerates key assumed set) — the same
        path Manage ▸ Currencies ▸ Refresh Now uses. Returns
        ``(count_or_None, errors)``; catches its own exception so Update All can
        keep going."""
        try:
            result = fx.refresh_latest_into(self._repo, force=True)
        except Exception as e:  # noqa: BLE001
            return None, [f"Could not update rates: {e}"]
        return result.new_rates_count, result.errors

    def _on_update_prices(self) -> None:
        """Toolbar Update Prices — fetch latest security prices directly, then
        refresh sidebar balances so changed market values show immediately.
        Routes to the Securities dialog when no Tiingo key is set yet."""
        if not self._has_setting("tiingo_api_key"):
            QMessageBox.information(
                self, "No Tiingo API key",
                "Add your Tiingo API token in Manage ▸ Securities… before "
                "updating prices. (Securities with no ticker can still be "
                "priced manually there.)",
            )
            self._on_manage_securities()
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            n, errors = self._refresh_prices()
        finally:
            QApplication.restoreOverrideCursor()
        self._refresh_sidebar_balances()
        self.statusBar().showMessage(
            f"Updated {self._count_phrase(n, 'price')}", 6000,
        )
        if errors:
            self._warn_refresh_errors(
                "Some prices failed", self._PRICE_ERR_PREAMBLE, errors,
            )

    def _on_update_rates(self) -> None:
        """Toolbar Update Rates — fetch latest FX rates directly, then refresh
        sidebar balances so converted figures update. Routes to the Currencies
        dialog when no openexchangerates key is set yet."""
        if not self._has_setting("oxr_api_key"):
            QMessageBox.information(
                self, "No exchange-rate API key",
                "Add your openexchangerates.org app_id in Manage ▸ "
                "Currencies… before updating rates.",
            )
            self._on_manage_currencies()
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            n, errors = self._refresh_rates()
        finally:
            QApplication.restoreOverrideCursor()
        self._refresh_sidebar_balances()
        self.statusBar().showMessage(
            f"Updated {self._count_phrase(n, 'rate')}", 6000,
        )
        if errors:
            self._warn_refresh_errors(
                "Some rates failed", self._RATE_ERR_PREAMBLE, errors,
            )

    def _on_update_all(self) -> None:
        """Toolbar Update All — refresh prices and FX rates in one click (the
        backlog's F2 "Update all"). Each is run only if its API key is set; a
        missing key is reported as skipped rather than popping a dialog (unlike
        the single-action buttons), so one click can't spawn two modal asks. A
        failure in one doesn't abort the other (each core catches its own).
        Bank feeds are deliberately excluded — they need interactive consent
        and live in their own Manage ▸ Bank Feeds dialog."""
        has_tiingo = self._has_setting("tiingo_api_key")
        has_oxr = self._has_setting("oxr_api_key")
        if not (has_tiingo or has_oxr):
            QMessageBox.information(
                self, "Nothing to update",
                "Add a Tiingo API token (Manage ▸ Securities…) for prices "
                "and/or an openexchangerates app_id (Manage ▸ Currencies…) for "
                "exchange rates, then Update All refreshes them in one click.",
            )
            return
        parts: list[str] = []
        skipped: list[str] = []
        errors: list = []
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            if has_tiingo:
                n, errs = self._refresh_prices()
                parts.append(self._count_phrase(n, "price"))
                errors += errs
            else:
                skipped.append("prices (no Tiingo key)")
            if has_oxr:
                n, errs = self._refresh_rates()
                parts.append(self._count_phrase(n, "rate"))
                errors += errs
            else:
                skipped.append("rates (no exchange-rate key)")
        finally:
            QApplication.restoreOverrideCursor()
        self._refresh_sidebar_balances()
        msg = "Updated " + ", ".join(parts)
        if skipped:
            msg += " · skipped " + ", ".join(skipped)
        self.statusBar().showMessage(msg, 8000)
        if errors:
            self._warn_refresh_errors(
                "Update finished with errors",
                "Update All finished with errors:", errors,
            )

    # ── menus ──

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Open…", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self._on_open)
        file_menu.addAction(open_action)

        save_copy_action = QAction("&Save Copy As…", self)
        save_copy_action.setShortcut(QKeySequence.SaveAs)
        save_copy_action.triggered.connect(self._on_save_copy_as)
        file_menu.addAction(save_copy_action)

        manage_data_action = QAction("Manage &Data…", self)
        manage_data_action.setShortcut(QKeySequence("Ctrl+Shift+D"))
        manage_data_action.triggered.connect(self._on_manage_data)
        file_menu.addAction(manage_data_action)

        file_menu.addSeparator()

        self._import_action = QAction("&Import…", self)
        self._import_action.setShortcut(QKeySequence("Ctrl+I"))
        self._import_action.triggered.connect(self._on_import)
        file_menu.addAction(self._import_action)

        self._import_latest_action = QAction("Import &Latest", self)
        self._import_latest_action.setShortcut(QKeySequence("Ctrl+Shift+I"))
        self._import_latest_action.setToolTip(
            "Import the newest statement file from this account's import folder"
        )
        self._import_latest_action.triggered.connect(self._on_import_latest)
        file_menu.addAction(self._import_latest_action)
        self.addAction(self._import_latest_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        txn_menu = self.menuBar().addMenu("&Transaction")

        self._new_txn_action = QAction("&New Transaction…", self)
        self._new_txn_action.setShortcut(QKeySequence.New)
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

        # Ctrl+Shift+I (not Ctrl+I — that's File ▸ Import). No StandardKey
        # exists for an account-summary action.
        self._account_summary_action = QAction("Account &Summary…", self)
        self._account_summary_action.setShortcut(QKeySequence("Ctrl+Shift+I"))
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

        # ADR-095: loans need their terms captured, so they get a dedicated verb.
        self._new_loan_action = QAction("New &Loan…", self)
        self._new_loan_action.triggered.connect(self._on_new_loan)
        account_menu.addAction(self._new_loan_action)

        self._edit_account_action = QAction("&Edit Account…", self)
        self._edit_account_action.triggered.connect(self._on_edit_account)
        account_menu.addAction(self._edit_account_action)

        # ADR-069: Close is the gentle, common verb (keeps history, leaves
        # Net Worth); Delete is the destructive one below it.
        self._close_account_action = QAction("&Close Account…", self)
        self._close_account_action.triggered.connect(self._on_close_account)
        account_menu.addAction(self._close_account_action)

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

        self._manage_rules_action = QAction("R&ules…", self)
        self._manage_rules_action.setToolTip(
            "Auto-categorisation rules — match payee/memo text to set a "
            "payee and/or category on import"
        )
        self._manage_rules_action.triggered.connect(self._on_manage_rules)
        manage_menu.addAction(self._manage_rules_action)

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

        self._manage_feeds_action = QAction("&Bank Feeds…", self)
        self._manage_feeds_action.setToolTip(
            "OFX Direct Connect — free auto-feed for US banks that support it"
        )
        self._manage_feeds_action.triggered.connect(self._on_manage_feeds)
        manage_menu.addAction(self._manage_feeds_action)

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

        self._income_over_time_action = QAction("&Income Over Time…", self)
        self._income_over_time_action.triggered.connect(
            self._on_income_over_time_report
        )
        reports_menu.addAction(self._income_over_time_action)

        self._income_expense_action = QAction("Income && &Expense…", self)
        self._income_expense_action.triggered.connect(
            self._on_income_expense_report
        )
        reports_menu.addAction(self._income_expense_action)

        self._payee_report_action = QAction("&Payee…", self)
        self._payee_report_action.triggered.connect(self._on_payee_report)
        reports_menu.addAction(self._payee_report_action)

        self._category_payee_action = QAction("&Category && Payee…", self)
        self._category_payee_action.triggered.connect(self._on_category_payee_report)
        reports_menu.addAction(self._category_payee_action)

        self._investment_returns_action = QAction("&Investment Returns…", self)
        self._investment_returns_action.triggered.connect(
            self._on_investment_returns_report
        )
        reports_menu.addAction(self._investment_returns_action)

        self._investment_income_action = QAction("Investment Inco&me…", self)
        self._investment_income_action.triggered.connect(
            self._on_investment_income_report
        )
        reports_menu.addAction(self._investment_income_action)

        self._sankey_action = QAction("&Sankey (Income → Expenses)…", self)
        self._sankey_action.triggered.connect(self._on_sankey_report)
        reports_menu.addAction(self._sankey_action)

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

        # ── View ▸ Appearance (ADR-076) ──
        view_menu = self.menuBar().addMenu("&View")
        self._dark_mode_action = QAction("&Dark Mode", self)
        self._dark_mode_action.setCheckable(True)
        self._dark_mode_action.setChecked(tokens.current_theme() == "dark")
        self._dark_mode_action.toggled.connect(self._on_toggle_dark_mode)
        view_menu.addAction(self._dark_mode_action)

        # ── Help ▸ Getting Started / About / licensing (ADR-079, ADR-098) ──
        help_menu = self.menuBar().addMenu("&Help")
        getting_started_action = QAction("&Getting Started…", self)
        getting_started_action.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(version.DOCS_URL))
        )
        help_menu.addAction(getting_started_action)
        website_action = QAction("&Visit Website…", self)
        website_action.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(version.WEBSITE_URL))
        )
        help_menu.addAction(website_action)
        help_menu.addSeparator()
        about_action = QAction("&About My Financial Life…", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)
        diagnostics_action = QAction("Export &Diagnostics…", self)
        diagnostics_action.triggered.connect(self._on_export_diagnostics)
        help_menu.addAction(diagnostics_action)
        help_menu.addSeparator()
        enter_license_action = QAction("Enter &License…", self)
        enter_license_action.triggered.connect(self._on_enter_license)
        help_menu.addAction(enter_license_action)
        buy_action = QAction("&Buy My Financial Life…", self)
        buy_action.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(license_service.BUY_URL))
        )
        help_menu.addAction(buy_action)

    def _on_about(self) -> None:
        """Help ▸ About — version + license state (ADR-079)."""
        from mfl_desktop.ui.about_dialog import AboutDialog
        AboutDialog(self).exec()
        self._refresh_license_cue()

    def _on_export_diagnostics(self) -> None:
        """Help ▸ Export Diagnostics (ADR-099) — write the local diagnostics
        blob (environment, paths, recent log) to a user-chosen file for a
        support email. Nothing is sent anywhere; the user controls the file."""
        from mfl_desktop import diagnostics
        default = str(
            QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
            or Path.home()
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export diagnostics",
            str(Path(default) / "mfl-diagnostics.txt"),
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        try:
            written = diagnostics.write_diagnostics(Path(path), repo=self._repo)
        except Exception as e:
            QMessageBox.warning(
                self, "Export failed",
                f"Could not write the diagnostics file:\n\n{e}",
            )
            return
        QMessageBox.information(
            self, "Diagnostics exported",
            f"Saved to:\n{written}\n\nAttach this to a support email. It "
            f"contains app + system info and recent log lines — no account "
            f"data or passwords.",
        )

    def _on_enter_license(self) -> None:
        """Help ▸ Enter License — paste + validate a key (ADR-079)."""
        from mfl_desktop.ui.license_dialog import LicenseDialog
        dlg = LicenseDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.installed is not None:
            self.statusBar().showMessage(
                f"License activated — thank you, {dlg.installed.name}!", 8000,
            )
            self._refresh_license_cue()

    def _maybe_show_license_nag(self) -> None:
        """On launch, gently surface licensing when the trial is ending or has
        ended (ADR-079 — friction, not a fortress). A licensed app and a trial
        with plenty of runway stay silent."""
        try:
            status = license_service.current_status()
        except Exception:
            return
        if status.state == STATE_EXPIRED:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("Your free trial has ended")
            box.setText(
                "Thanks for trying My Financial Life. Your free trial has "
                "ended — buy a license to keep your financial life going."
            )
            box.setInformativeText(
                "Your data is safe and untouched; entering a license unlocks "
                "everything again."
            )
            buy = box.addButton("Buy…", QMessageBox.AcceptRole)
            enter = box.addButton("Enter license…", QMessageBox.ActionRole)
            box.addButton("Continue", QMessageBox.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is buy:
                QDesktopServices.openUrl(QUrl(license_service.BUY_URL))
            elif clicked is enter:
                self._on_enter_license()
        elif status.state == STATE_TRIAL and status.trial_days_left <= 7:
            n = status.trial_days_left
            self.statusBar().showMessage(
                f"Free trial — {n} day{'s' if n != 1 else ''} left. "
                f"Help ▸ Buy to purchase a license.", 10000,
            )
        self._refresh_license_cue()

    def _refresh_license_cue(self) -> None:
        """Reflect license state in the window title (a quiet, always-visible
        cue). Licensed = clean title; trial/expired get a short suffix."""
        try:
            status = license_service.current_status()
        except Exception:
            return
        self._license_title_suffix = ""
        if status.state == STATE_TRIAL:
            self._license_title_suffix = (
                f" — Trial ({status.trial_days_left}d left)"
            )
        elif status.state == STATE_EXPIRED:
            self._license_title_suffix = " — Trial ended"
        self._update_window_title()

    def _on_toggle_dark_mode(self, on: bool) -> None:
        """ADR-076: switch the app theme live and persist the choice."""
        theme = "dark" if on else "light"
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, theme)
        try:
            self._repo.set_setting(THEME_SETTING_KEY, theme)
        except Exception:
            pass

    # ── new / delete transaction ──

    def _payee_default_category_for_name(self, name: str):
        """ADR-073/ADR-106: resolve a typed payee name to a category to
        pre-fill in the New Transaction dialog. Prefers the explicit
        remembered auto-category; when none is set, falls back to the payee's
        most-common historical category so a payee you've categorised before
        (but never explicitly 'remembered') still pre-fills. None if neither
        resolves."""
        pid = self._repo.find_payee_id_by_name(name)
        if pid is None:
            return None
        explicit = self._repo.get_payee_default_category(pid)
        if explicit is not None:
            return explicit
        return self._repo.most_common_category_for_payee(pid)

    def _on_new_transaction(self) -> None:
        accounts = self._repo.list_accounts()
        if not accounts:
            QMessageBox.information(
                self, "No accounts",
                "Create an account before adding transactions.",
            )
            return
        # ADR-048: on an investment account, New Transaction opens the
        # investment form (Buy/Sell/Div/…) rather than the cash dialog.
        if self._account is not None and self._account.family == "investment":
            # ADR-107: loop while the user keeps clicking "Save & New".
            while self._open_investment_txn_dialog(seed=None):
                pass
            return
        default_id = self._account.id if self._account is not None else None
        # ADR-105: loop while the user keeps clicking "Save & New", reusing the
        # just-used account as the default for the next entry.
        while True:
            next_default = self._create_one_transaction(default_id)
            if next_default is None:
                return
            default_id = next_default

    def _create_one_transaction(self, default_id: Optional[int]) -> Optional[int]:
        """Show the New Transaction dialog once and commit its result.

        Returns the account id to reuse for another entry when the user clicked
        Save & New, or None to stop (plain Save / Split / cancel / error).
        Factored out of :meth:`_on_new_transaction` so the Save & New loop
        reuses the full commit path — transfer, split, payee-default-category —
        unchanged (ADR-105)."""
        accounts = self._repo.list_accounts()
        dialog = NewTransactionDialog(
            accounts=accounts,
            categories=self._categories,
            default_account_id=default_id,
            payee_category_lookup=self._payee_default_category_for_name,
            payee_names=self._repo.list_payee_names(),
            parent=self,
        )
        if dialog.exec() != NewTransactionDialog.Accepted:
            return None
        values = dialog.values()
        if values is None:
            return None

        # ADR-051: the user clicked "Split…" — hand the header fields and the
        # entered amount to the split dialog (which collects the category lines
        # and persists). Splits aren't supported on investment accounts.
        if dialog.split_requested():
            self._open_split_txn_dialog(
                account_id=values.account_id,
                prefill={
                    "posted_date": values.posted_date,
                    "payee_name": values.payee_name,
                    "status": values.status,
                    "memo": values.memo,
                    "total_amount": values.amount,
                },
            )
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
            return values.account_id if dialog.save_and_new_requested() else None

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
        return values.account_id if dialog.save_and_new_requested() else None

    def _on_table_double_clicked(self, proxy_index) -> None:
        """Double-click routing for dialog-edited rows:

        - ADR-048: an investment row opens the investment edit dialog.
        - ADR-051: a split row opens the split edit dialog (works in both the
          single-account and All-transactions views).

        Plain cash rows are inline-editable, so Qt's own double-click edit
        trigger handles them and this is a no-op for them."""
        if not proxy_index.isValid():
            return
        source_index = self._proxy.mapToSource(proxy_index)
        row = self._model.row_at(source_index.row())
        if self._account is not None and self._account.family == "investment":
            if row.action is None:
                return          # a plain cash row that happens to sit here
            # ADR-086: the Category cell is inline-editable for cash income/
            # expense actions — don't hijack its double-click to open the dialog.
            col_name = self._model.COLUMNS[source_index.column()][1]
            if col_name == "category_name" and is_categorisable(row.action):
                return
            self._open_investment_txn_dialog(seed=row)
            return
        if row.split_count:
            self._open_split_txn_dialog(seed=row)

    def _open_split_txn_dialog(
        self, seed=None, prefill=None, account_id=None,
    ) -> None:
        """Open the split dialog (ADR-051) in edit (``seed``) or create
        (``prefill``) mode against a cash/credit account, then reload on save.
        Resolves the account from the seed row / explicit id / current view."""
        aid = account_id
        if aid is None and seed is not None:
            aid = seed.account_id
        if aid is None and self._account is not None:
            aid = self._account.id
        if aid is None:
            return
        account = self._repo.get_account_by_id(aid)
        if account is None:
            return
        if account.family == "investment":
            QMessageBox.information(
                self, "Split transaction",
                "Split transactions aren't supported on investment accounts.",
            )
            return
        # Reconciled rows get the same "change anyway?" confirm as inline edits.
        if seed is not None and self._repo.is_reconciled(seed.id):
            if not self._confirm_reconciled_edit(seed.id):
                return
        dialog = SplitTransactionDialog(
            self._repo, account, self._categories,
            seed=seed, prefill=prefill, parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        self._model.reload()
        self._refresh_sidebar_balances()
        self.statusBar().showMessage(
            "Split updated" if seed is not None else "Split added", 4000,
        )

    def _open_investment_txn_dialog(self, seed) -> bool:
        """Open the investment transaction dialog in create (seed=None) or edit
        mode, then reload the register + sidebar on a successful save.

        Returns True when the user clicked "Save & New" (create mode only), so
        the caller should reopen a fresh dialog for the next entry (ADR-107)."""
        if self._account is None:
            return False
        dialog = InvestmentTransactionDialog(
            self._repo, self._account, seed=seed, parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return False
        self._model.reload()
        self._refresh_sidebar_balances()
        self.statusBar().showMessage(
            "Transaction updated" if seed is not None else "Transaction added",
            4000,
        )
        return seed is None and dialog.save_and_new_requested()

    def _on_model_data_changed(self, top_left, _bottom_right, _roles) -> None:
        """Detect inline edits that need post-processing:

        - Category set to a transfer-kind value → pop the destination
          prompt (ADR-020).
        - Amount or Date changed → reload the model so running balances
          recompute, and refresh sidebar balances.

        Other edits — payee, status, memo — pass through without action."""
        col_idx = top_left.column()
        if col_idx < 0 or col_idx >= len(self._model.COLUMNS):
            return
        col_name = self._model.COLUMNS[col_idx][1]
        if col_name in ("amount", "posted_date"):
            # Running balance and sidebar totals are stale after an amount edit;
            # a date edit additionally reorders the row (date is the running-
            # balance sort key). Reload picks up the new running balances from
            # the Repository (computed in list order) and re-sums the sidebar.
            self._model.reload()
            self._refresh_sidebar_balances()
            return
        if col_name != "category_name":
            return
        row = self._model.row_at(top_left.row())
        if row.transfer_id is not None:
            return
        if self._category_kind(row.category_id) != "transfer":
            # ADR-072: a plain category edit is the natural moment to offer to
            # remember the payee→category mapping for future imports.
            self._maybe_offer_memorise(row)
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
        """Refresh the cached category list without touching the model or the
        typeahead delegate. Safe to call from inside a delegate's
        setModelData."""
        self._categories = self._repo.list_categories_flat()

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

    def _category_label(self, category_id: int) -> str:
        """Full breadcrumb path for a category id from the window cache,
        falling back to the bare id if it's missing."""
        for c in self._categories:
            if c.id == category_id:
                return c.path or c.name
        return f"category {category_id}"

    def _maybe_offer_memorise(self, row) -> None:
        """ADR-072: after a plain (non-transfer) inline category edit, offer
        to remember the payee→category mapping — but only when the payee has
        no memory yet (changing an existing memory is a Payees-dialog job, so
        a deliberate one-off override doesn't re-prompt)."""
        if row.payee_id is None:
            return
        cat_id = row.category_id
        if cat_id is None or cat_id == self._repo.uncategorised_id():
            return
        try:
            if self._repo.get_payee_default_category(row.payee_id) is not None:
                return
            existing = self._repo.count_uncategorised_for_payee(row.payee_id)
        except Exception:
            return
        payee_name = row.payee_name or "this payee"
        dialog = MemoriseCategoryDialog(
            payee_name, self._category_label(cat_id), existing, parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._repo.set_payee_default_category(row.payee_id, cat_id)
            if dialog.apply_to_existing():
                changed = self._repo.apply_default_category_to_uncategorised(
                    row.payee_id, cat_id,
                )
                if changed:
                    self._model.reload()
                    self._refresh_sidebar_balances()
        except Exception as e:
            QMessageBox.critical(self, "Could not save", str(e))

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
        # ADR-048: if every selected row is the SAME investment security, offer
        # a Symbol field that re-tickers that security (the ticker lives on the
        # security master, so it only makes sense for a single-security set).
        security_context = None
        sec_ids: set[int] = set()
        a_sec_row = None
        split_parent_ids: list[int] = []        # ADR-051
        for proxy_idx in self._table.selectionModel().selectedRows():
            src = self._proxy.mapToSource(proxy_idx)
            if not src.isValid():
                continue
            r = self._model.row_at(src.row())
            if r.security_id is not None:
                sec_ids.add(r.security_id)
                a_sec_row = r
            if r.split_count:
                split_parent_ids.append(r.id)
        if len(sec_ids) == 1 and a_sec_row is not None:
            security_context = (
                a_sec_row.security_id,
                a_sec_row.security_name,
                a_sec_row.security_symbol,
            )

        dialog = BulkEditDialog(
            self._categories,
            len(ids),
            payee_names=self._repo.list_payee_names(),
            security_context=security_context,
            parent=self,
        )
        if dialog.exec() != BulkEditDialog.Accepted:
            return
        changes = dialog.values()
        if not changes:
            return

        # Symbol is a security-master edit, not a txn field — apply it through
        # update_security and strip it before the bulk txn update.
        new_symbol = changes.pop("symbol", None)
        if new_symbol is not None and security_context is not None:
            try:
                self._repo.update_security(security_context[0], symbol=new_symbol)
            except ValueError as e:
                QMessageBox.warning(self, "Bulk edit", str(e))
                return
        if not changes:
            # Only the symbol was changed — reload so the Symbol column updates.
            self._model.reload()
            self.statusBar().showMessage("Symbol updated", 4000)
            return

        # ADR-020 category-driven transfers: if the user picked a
        # transfer-kind category, prompt for the destination and convert
        # every selected row into a transfer (plus apply any other ticked
        # fields). Otherwise the existing bulk_update path runs as before.
        new_category_id = changes.get("category_id")

        # ADR-051: a split parent has no single category. If the user is setting
        # a category and the selection includes split transactions, converting
        # them means discarding their lines — confirm first. (And they can't
        # become transfers — a split is never a transfer.)
        if new_category_id is not None and split_parent_ids:
            if self._category_kind(new_category_id) == "transfer":
                QMessageBox.warning(
                    self, "Bulk edit",
                    f"{len(split_parent_ids)} of the selected transactions are "
                    "split — split transactions can't be converted to "
                    "transfers. Remove them from the selection first.",
                )
                return
            resp = QMessageBox.question(
                self, "Bulk edit",
                f"{len(split_parent_ids)} of the selected transactions are "
                "split. Setting a category will convert them to a "
                "single-category transaction and discard their split lines.\n\n"
                "Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
            try:
                for sid in split_parent_ids:
                    self._repo.convert_split_to_plain(sid, new_category_id)
            except Exception as e:
                QMessageBox.critical(
                    self, "Bulk edit",
                    f"The split conversion was not applied:\n\n{e}",
                )
                return
            # Those rows are now plain; the bulk update below re-applies the
            # (same) category plus any payee/status/memo changes uniformly.

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
        """Replace the working repository with a freshly opened one (File ▸ Open).

        Brings the new repo's UI up first, then tears the old one down, so a
        partial-failure path can still recover. The opened file *becomes* the
        live file — edits flow straight into it. (Contrast ``_load_dataset``,
        which clones onto the working file and leaves the source pristine.)
        """
        old_repo = self._repo
        self._loaded_dataset = None
        self._adopt_repository(new_repo)
        self._teardown_repository(old_repo)
        # The opened file is now the live file — reopen it on next launch
        # (ADR-092). A loaded dataset (``_load_dataset``) deliberately keeps
        # the same working file, so its path is already what's remembered.
        remember_last_db(new_repo.db_path)

    def _adopt_repository(self, new_repo: Repository) -> None:
        """Make ``new_repo`` the live repo and rebuild every UI surface that
        depends on it: sidebar (accounts, folders, balances), the cached
        category list, the filter-bar combo, and the register model. Also
        re-runs the per-file auto-post sweep against the freshly-opened DB."""
        self._repo = new_repo
        self._service = ImportService(new_repo)
        self._categories = new_repo.list_categories_flat()
        self._account = None
        # The Home dashboard holds its own repo reference (ADR-075); repoint it
        # at the newly-opened file and rebuild, otherwise it keeps reading the
        # old — now closed — repo and shows stale data until restart.
        self._home_view.set_repo(new_repo)
        self._home_view.refresh()
        # _reload_sidebar pulls fresh data via self._repo (= new_repo) and
        # selects an item; the sidebar's selection_changed signal then drives
        # _show_account / _show_all_transactions which rebuild the model.
        self._reload_sidebar(select_iri=None)
        self._update_window_title()
        # Retention policy is per-file (ADR-060), so re-arm the capture timer to
        # the newly-adopted file's cadence.
        self._apply_snapshot_interval()
        # Schedules are per-file, so a different file means a different set of
        # due auto-posters to materialise (or none, for a fresh DB).
        self._run_auto_post_sweep()

    def _teardown_repository(self, old_repo: Repository) -> None:
        """Leave the file we're done with self-contained + backed up (ADR-057),
        the same way closeEvent treats the live file on quit, then close it."""
        snapshots.maybe_snapshot(old_repo)
        old_repo.checkpoint()
        old_repo.close()

    def _on_manage_data(self) -> None:
        dialog = DataLibraryDialog(self._repo, parent=self)
        # Loading replaces the live working file — only the window can drive
        # that, so the dialog asks us to do it via load_requested.
        dialog.load_requested.connect(self._load_dataset)
        # Re-arm the capture timer if the user changed the cadence (ADR-060).
        dialog.settings_changed.connect(self._apply_snapshot_interval)
        # ADR-109 Locations: main-file + snapshots-folder changes the window owns.
        dialog.open_existing_main_requested.connect(self._on_open_existing_main)
        dialog.relocate_main_requested.connect(self._relocate_main_file)
        dialog.snapshots_root_changed.connect(self._set_snapshots_root)
        dialog.exec()

    def _on_open_existing_main(self, path: Path) -> None:
        """Make an existing file the live working file (ADR-109 Locations) — the
        same swap as File ▸ Open, which already records it as the file to reopen
        next launch."""
        path = Path(path)
        if path.resolve() == self._repo.db_path.resolve():
            return
        try:
            new_repo = Repository(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(
                self, "Could not open file",
                f"The file at {path} could not be opened as a "
                f"My Financial Life database:\n\n{e}",
            )
            return
        self._swap_repository(new_repo)
        self.statusBar().showMessage(f"Opened {path.name}", 5000)

    def _relocate_main_file(self, target_dir: Path) -> None:
        """Move the live working file into ``target_dir`` (ADR-109 Locations).

        Done as a self-contained copy + verify + swap + best-effort delete of the
        original — never a bare ``os.rename`` (Documents → external drive is a
        cross-device move that would raise). The copy is atomic via
        ``Repository.save_copy``, so any failure leaves the original file live
        and intact."""
        target_dir = Path(target_dir)
        dest = target_dir / self._repo.db_path.name
        src = self._repo.db_path.resolve()
        if dest.resolve() == src:
            return
        if dest.exists():
            overwrite = QMessageBox.question(
                self, "Replace file",
                f"A file named “{dest.name}” already exists in that folder. "
                "Replace it?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if overwrite != QMessageBox.Yes:
                return
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            self._repo.checkpoint()        # fold WAL in so the copy is complete
            self._repo.save_copy(dest)     # atomic online backup to the new home
            Repository(dest).close()       # verify it opens before we commit
            new_repo = Repository(dest)
        except Exception as e:  # noqa: BLE001 — original is untouched on failure
            QMessageBox.critical(
                self, "Move failed",
                f"Could not move the data file to {target_dir}:\n\n{e}",
            )
            return
        self._swap_repository(new_repo)    # adopts new, tears down + closes old
        # Best-effort: remove the file we moved away from + its WAL/SHM sidecars.
        for stray in (src, src.with_name(src.name + "-wal"),
                      src.with_name(src.name + "-shm")):
            try:
                stray.unlink()
            except OSError:
                pass
        self.statusBar().showMessage(f"Moved data file to {target_dir}", 6000)

    def _set_snapshots_root(self, parent: Path) -> None:
        """Point backups at a new parent folder (ADR-109 Locations) and seed the
        new ``MFL Snapshots`` location immediately with a forced capture."""
        set_snapshots_root(Path(parent))
        snapshots.maybe_snapshot(self._repo, force=True)
        self.statusBar().showMessage(
            f"Backups now saved under {parent}", 6000,
        )

    def _load_dataset(self, source: Path) -> None:
        """Load a saved dataset / snapshot as a *fresh working copy* (ADR-059).

        Unlike File ▸ Open (which makes the picked file the live file), this
        clones ``source`` onto the current working file so the saved original
        stays pristine — load a baseline, edit it, reload it clean. The current
        working file is snapshotted, checkpointed, and closed first; then the
        clone overwrites it (atomic temp-and-replace, so a clone failure leaves
        the working file intact); then we reopen and rebuild the UI.
        """
        bench = self._repo.db_path
        # Tear down the working file *before* overwriting it — its connection
        # has to be closed before clone_database can replace the file on disk.
        self._teardown_repository(self._repo)
        try:
            data_library.clone_database(source, bench)
        except Exception as e:  # noqa: BLE001 — recover by reopening the bench
            # The clone is atomic, so on failure the working file is untouched.
            # Reopen it so the user isn't left with a dead window.
            self._adopt_repository(Repository(bench))
            QMessageBox.critical(
                self, "Load failed",
                f"Could not load “{source.stem}”:\n\n{e}",
            )
            return
        self._loaded_dataset = source.stem
        self._adopt_repository(Repository(bench))
        self.statusBar().showMessage(
            f"Loaded a working copy of {source.stem}", 5000,
        )

    # ── import ──

    # ── per-account import memory + Import latest (ADR-077 Track 1) ──

    _IMPORT_EXTS = (".ofx", ".qfx", ".qif", ".csv")

    def _import_dir_for_account(self) -> str:
        """The folder to open the import picker in for the current account:
        the one last imported from (remembered per account), else the OS
        Downloads folder, else home."""
        if self._account is not None:
            saved = self._repo.get_setting(f"import_dir:{self._account.id}")
            if saved and Path(saved).is_dir():
                return saved
        downloads = QStandardPaths.writableLocation(
            QStandardPaths.DownloadLocation
        )
        if downloads and Path(downloads).is_dir():
            return downloads
        return str(Path.home())

    def _remember_import_dir(self, path: str) -> None:
        if self._account is not None:
            try:
                self._repo.set_setting(
                    f"import_dir:{self._account.id}", str(Path(path).parent),
                )
            except Exception:
                pass

    def _newest_import_file(self, folder: str) -> Optional[Path]:
        """Newest file with a recognised statement extension in ``folder``."""
        try:
            candidates = [
                p for p in Path(folder).iterdir()
                if p.is_file() and p.suffix.lower() in self._IMPORT_EXTS
            ]
        except OSError:
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _on_import_latest(self) -> None:
        if self._account is None:
            QMessageBox.information(
                self, "Pick an account",
                "Select an account in the sidebar first — imports always "
                "target a specific account.",
            )
            return
        folder = self._import_dir_for_account()
        newest = self._newest_import_file(folder)
        if newest is None:
            QMessageBox.information(
                self, "Nothing to import",
                f"No OFX / QFX / QIF / CSV file found in:\n{folder}\n\n"
                f"Download a statement there (or use Import… to pick one "
                f"elsewhere — that folder is then remembered for this account).",
            )
            return
        when = datetime.fromtimestamp(newest.stat().st_mtime)
        if QMessageBox.question(
            self, "Import latest",
            f"Import the newest statement into {self._account.name}?\n\n"
            f"{newest.name}\n({folder} · {when:%-d %b %Y %H:%M})",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        ) != QMessageBox.Yes:
            return
        self._import_file(str(newest))

    def _on_import(self) -> None:
        if self._account is None:
            QMessageBox.information(
                self, "Pick an account",
                "Select an account in the sidebar first — imports always "
                "target a specific account.",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import transactions", self._import_dir_for_account(),
            "Bank statements (*.ofx *.qfx *.qif *.csv);;All files (*)",
        )
        if not path:
            return
        self._remember_import_dir(path)
        self._import_file(path)

    def _import_file(self, path: str) -> None:
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
        # ADR-085: when the cross-source duplicate pass flagged any rows, open
        # the review dialog so the user decides which to skip/merge. A clean
        # import (no matches) still commits silently — nothing to ask.
        matches = [
            tx for tx in pending.transactions
            if tx.status == "potential_match"
        ]
        if matches:
            review = ImportReviewDialog(pending, parent=self)
            if review.exec() != QDialog.Accepted:
                self._service.discard_pending(token)
                return
            accepted = review.accepted_fitids()
        else:
            accepted = set()
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
        cached choice list, and the category delegate.
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
                credit_limit=values.credit_limit,
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Could not create account",
                f"The account was not created:\n\n{e}",
            )
            return
        self._reload_sidebar(select_iri=acct.iri)
        self.statusBar().showMessage(f"Created account {acct.name!r}", 4000)

    def _on_new_loan(self) -> None:
        """Create a loan account via the loan dialog (ADR-095), then open its
        Account Summary (where the amortization schedule lives)."""
        from mfl_desktop.ui.loan_dialog import LoanDialog
        dialog = LoanDialog(self._repo, account_id=None, parent=self)
        if dialog.exec() != QDialog.Accepted or dialog.created_account_id is None:
            return
        acct_id = dialog.created_account_id
        acct = self._repo.get_account_by_id(acct_id)
        self._reload_sidebar(select_iri=acct.iri if acct else None)
        self.statusBar().showMessage(
            f"Created loan {acct.name!r}" if acct else "Created loan", 4000,
        )
        self._open_account_summary(acct_id)

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
                credit_limit=values.credit_limit,
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

    def _on_close_account(self) -> None:
        """Soft-close the selected account (ADR-069). Non-destructive: it
        moves to the 'Closed accounts' group and drops out of Net Worth, but
        its transactions and history are kept and reopening is one click."""
        if self._account is None or self._account.is_closed:
            return
        acct = self._account
        confirm = QMessageBox.question(
            self, "Close account",
            f"Close account {acct.name!r}?\n\n"
            "It will move to the 'Closed accounts' group and be excluded from "
            "Net Worth and account pickers. Its transactions and history are "
            "kept, and you can reopen it any time (right-click it in the "
            "sidebar).",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._repo.close_account(acct.id)
        except Exception as e:
            QMessageBox.critical(
                self, "Could not close account",
                f"The account was not closed:\n\n{e}",
            )
            return
        # Drop the now-closed account from the active view — fall back to
        # All transactions rather than re-selecting the closed row.
        self._reload_sidebar(select_iri=None)
        self.statusBar().showMessage(f"Closed account {acct.name!r}", 4000)

    def _on_reopen_account(self, account_iri: str) -> None:
        """Reverse a close (ADR-069) — clear the archive flag and re-select."""
        acct = self._repo.get_account_by_iri(account_iri)
        if acct is None or not acct.is_closed:
            return
        try:
            reopened = self._repo.reopen_account(acct.id)
        except Exception as e:
            QMessageBox.critical(
                self, "Could not reopen account",
                f"The account was not reopened:\n\n{e}",
            )
            return
        self._reload_sidebar(select_iri=reopened.iri)
        self.statusBar().showMessage(f"Reopened account {reopened.name!r}", 4000)

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
        accounts = self._repo.list_accounts(include_closed=True)
        folders = self._repo.list_folders()
        balances = self._repo.compute_account_values(include_closed=True)
        reports = self._repo.list_reports()
        report_folders = self._repo.list_report_folders()
        self._sidebar.reload(
            accounts, folders, balances,
            reports=reports, report_folders=report_folders,
        )
        open_accounts = [a for a in accounts if not a.is_closed]
        if select_iri is not None:
            self._select_account_in_sidebar(select_iri)
        elif open_accounts:
            self._select_account_in_sidebar(open_accounts[0].iri)
        else:
            self._sidebar.select_all_transactions()
            self._show_all_transactions()

    def _refresh_sidebar_keep_selection(self) -> None:
        """Reload the sidebar (reports included) preserving whatever the
        user has selected. Used after a saved-report create / update /
        delete so the Reports section reflects the new state without
        moving the user's focus."""
        accounts = self._repo.list_accounts(include_closed=True)
        folders = self._repo.list_folders()
        balances = self._repo.compute_account_values(include_closed=True)
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
        if kind == "closed_group":
            # The 'Closed accounts' grouping row itself — just let the click
            # toggle it; no verbs of its own (ADR-069).
            return
        if kind == "account":
            iri = item.data(0, Qt.UserRole)
            # Make this the current selection so Edit/Delete operate on it.
            self._select_account_in_sidebar(iri)
            is_closed = bool(item.data(0, CLOSED_ROLE))
            menu = QMenu(self._sidebar)
            # Summary first — it's the verb a Banktivity user reaches for
            # most often from a sidebar right-click (ADR-033).
            menu.addAction(self._account_summary_action)
            menu.addSeparator()
            if is_closed:
                # A closed account (ADR-069): Reopen is the primary verb;
                # Edit / Move / Close don't apply while archived.
                reopen_act = menu.addAction("Reopen Account")
                reopen_act.triggered.connect(
                    lambda checked=False, target=iri: self._on_reopen_account(target)
                )
                menu.addSeparator()
                menu.addAction(self._delete_account_action)
            else:
                menu.addAction(self._new_account_action)
                menu.addAction(self._edit_account_action)
                menu.addSeparator()
                move_menu = menu.addMenu("&Move to Folder")
                self._populate_move_to_folder_menu(move_menu, iri)
                menu.addSeparator()
                menu.addAction(self._close_account_action)
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

    def _on_manage_rules(self) -> None:
        # ADR-073: a retroactive apply re-points payee/category on existing
        # rows, so reload the register when the rules dialog changes anything.
        dialog = RulesDialog(self._repo, parent=self)
        dialog.rules_changed.connect(self._model.reload)
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

    def _on_manage_feeds(self) -> None:
        """Open Manage → Bank Feeds… (ADR-077). The unified dialog manages
        feeds across all providers (OFX Direct Connect, Enable Banking,
        SimpleFIN, Plaid) and runs Update, which stages fetched transactions
        through the same import path (dedup/match/commit) as a file. We refresh
        the register + sidebar afterward if anything was actually imported."""
        dialog = BankFeedsDialog(
            self._repo, self._service,
            on_updated=self._refresh_after_feed_update, parent=self,
        )
        dialog.exec()

    def _refresh_after_feed_update(self) -> None:
        self._refresh_categories_view()
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
        self._refresh_schedules_cue()

    def _refresh_schedules_cue(self) -> None:
        """Decorate the register's Schedules button with the overdue /
        due-soon cue (A6, ADR-063).

        Re-queries every time rather than caching — there are only a
        handful of schedules, so the cost is negligible and it can't go
        stale. Plain label when nothing's pending; an amber ``● N`` when
        items are due within 3 days; a red ``⚠ (N)`` (N = overdue +
        due-soon) the moment anything is overdue. Best-effort: a DB error
        resets to the plain label rather than refusing to paint the bar."""
        if not hasattr(self, "_schedules_btn"):
            return
        try:
            schedules = self._repo.list_scheduled_txns()
        except Exception:
            self._schedules_btn.setText(self._SCHEDULES_LABEL)
            self._schedules_btn.setStyleSheet("")
            self._schedules_btn.setToolTip("")
            return

        summary = bills_due_summary(schedules, date.today())
        if not summary.has_alert:
            self._schedules_btn.setText(self._SCHEDULES_LABEL)
            self._schedules_btn.setStyleSheet("")
            self._schedules_btn.setToolTip("Manage scheduled transactions")
            return

        if summary.overdue:
            # Red — something's already past due; surface the whole count
            # (overdue + due-soon) so the figure matches the dialog the
            # click opens.
            self._schedules_btn.setText(
                f"⚠ {self._SCHEDULES_LABEL} ({summary.total})"
            )
            tokens.themed(self._schedules_btn, "QPushButton { color: {negative_strong}; font-weight: 600; }")
        else:
            # Amber — nothing overdue yet, just a heads-up for the next 3 days.
            self._schedules_btn.setText(
                f"{self._SCHEDULES_LABEL} ● {summary.due_soon}"
            )
            tokens.themed(self._schedules_btn, "QPushButton { color: {warning}; font-weight: 600; }")

        bits: list[str] = []
        if summary.overdue:
            bits.append(f"{summary.overdue} overdue")
        if summary.due_soon:
            bits.append(f"{summary.due_soon} due within 3 days")
        self._schedules_btn.setToolTip(" · ".join(bits))

    def _run_auto_post_sweep(self) -> None:
        """Materialise any auto-post schedules whose next-due date has
        already arrived. Idempotent: each post advances next_due_date
        past today, so re-running on the same launch posts nothing.

        Per-schedule failures are no longer hidden (ADR-091): the sweep
        returns them, and we surface a warning so a permanently-broken
        schedule (e.g. transfer-kind with no destination) can't keep
        silently missing every launch. A clean run stays quiet."""
        try:
            result = self._repo.auto_post_due(date.today().isoformat())
        except Exception:
            # The whole sweep failed (unexpected DB error). Don't refuse
            # to launch over it — the user can still use the app and
            # see the schedules via the dialog.
            return
        if result.posted:
            self._model.reload()
            self._refresh_sidebar_balances()
            self.statusBar().showMessage(
                f"Auto-posted {len(result.posted)} scheduled "
                f"transaction{'s' if len(result.posted) != 1 else ''}.", 8000,
            )
        if result.failures:
            n = len(result.failures)
            lines = "\n".join(
                f"• {f.label}: {f.reason}" for f in result.failures
            )
            QMessageBox.warning(
                self, "Some schedules couldn't auto-post",
                f"{n} scheduled transaction{'s' if n != 1 else ''} couldn't "
                f"be posted automatically and {'were' if n != 1 else 'was'} "
                f"skipped:\n\n{lines}\n\nOpen Schedules to fix "
                f"{'them' if n != 1 else 'it'} — e.g. set a destination "
                f"account on a transfer schedule, then Post Now.",
            )

    # ── reports ──

    def _on_spending_report(self) -> None:
        """Reports menu → Spending Over Time. Opens the *bare* window
        (no saved-state attached) — saved reports open via the sidebar
        instead (ADR-039 §reports-menu)."""
        self._open_bare_report(TYPE_SPENDING_OVER_TIME)

    def _on_income_over_time_report(self) -> None:
        """Reports menu → Income Over Time. Opens the *bare* window
        (ADR-088)."""
        self._open_bare_report(TYPE_INCOME_OVER_TIME)

    def _on_income_expense_report(self) -> None:
        """Reports menu → Income & Expense. Opens the *bare* window
        (ADR-064)."""
        self._open_bare_report(TYPE_INCOME_EXPENSE)

    def _on_payee_report(self) -> None:
        """Reports menu → Payee. Opens the *bare* window (ADR-066)."""
        self._open_bare_report(TYPE_PAYEE)

    def _on_category_payee_report(self) -> None:
        """Reports menu → Category & Payee. Opens the *bare* window (ADR-068)."""
        self._open_bare_report(TYPE_CATEGORY_PAYEE)

    def _on_investment_returns_report(self) -> None:
        """Reports menu → Investment Returns. Opens the *bare* window
        (ADR-046)."""
        self._open_bare_report(TYPE_INVESTMENT_RETURNS)

    def _on_sankey_report(self) -> None:
        """Reports menu → Sankey. Opens the *bare* window (ADR-056)."""
        self._open_bare_report(TYPE_SANKEY)

    def _on_investment_income_report(self) -> None:
        """Reports menu → Investment Income (ADR-108). A live analysis window
        (not a saved report), kept singleton — repeat clicks raise the existing
        one rather than stacking duplicates."""
        existing = self._investment_income_win
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        win = InvestmentIncomeWindow(self._repo, parent=self)
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(
            lambda _obj=None: setattr(self, "_investment_income_win", None)
        )
        self._investment_income_win = win
        win.show()

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
        elif type_key == TYPE_INCOME_OVER_TIME:
            win = IncomeReportWindow.open_bare(self._repo, parent=self)
        elif type_key == TYPE_INCOME_EXPENSE:
            win = IncomeExpenseWindow.open_bare(self._repo, parent=self)
        elif type_key == TYPE_INVESTMENT_RETURNS:
            win = InvestmentReturnsWindow.open_bare(self._repo, parent=self)
        elif type_key == TYPE_SANKEY:
            win = SankeyReportWindow.open_bare(self._repo, parent=self)
        elif type_key == TYPE_PAYEE:
            win = PayeeReportWindow.open_bare(self._repo, parent=self)
        elif type_key == TYPE_CATEGORY_PAYEE:
            win = CategoryPayeeWindow.open_bare(self._repo, parent=self)
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
        elif report.type == TYPE_INCOME_OVER_TIME:
            win = IncomeReportWindow.load_from_id(
                self._repo, report_id, parent=self,
            )
        elif report.type == TYPE_INCOME_EXPENSE:
            win = IncomeExpenseWindow.load_from_id(
                self._repo, report_id, parent=self,
            )
        elif report.type == TYPE_INVESTMENT_RETURNS:
            win = InvestmentReturnsWindow.load_from_id(
                self._repo, report_id, parent=self,
            )
        elif report.type == TYPE_SANKEY:
            win = SankeyReportWindow.load_from_id(
                self._repo, report_id, parent=self,
            )
        elif report.type == TYPE_PAYEE:
            win = PayeeReportWindow.load_from_id(
                self._repo, report_id, parent=self,
            )
        elif report.type == TYPE_CATEGORY_PAYEE:
            win = CategoryPayeeWindow.load_from_id(
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
        # Clicking an account slice opens its Account Summary via the
        # canonical single-instance path (ADR-083).
        win.account_activated.connect(self._open_account_summary)
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
        # parent=None (NOT self): the budget window opens app-modal dialogs
        # (Setup, chooser). On macOS a *child* window is hidden by the OS while
        # its parent isn't the key window — so when one of those dialogs takes
        # key, the register loses key and macOS hid the budget window (no Qt
        # close/destroy — just a vanish). As an independent top-level window it
        # stays visible behind an app-modal dialog. (ADR-058 close-on-save bug.)
        win = BudgetWindow(self._repo)
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
        """Account → Summary…/Ctrl+Shift+I handler. Opens the summary for the
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
