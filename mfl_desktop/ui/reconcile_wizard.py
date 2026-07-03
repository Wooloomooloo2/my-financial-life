"""Statement reconciliation wizard (ADR-040 + 2026-06-07 amendment).

A two-page dialog that walks one reconciliation pass for a single account:

  Page 1 — dates + balances. Starting/ending dates and the bank's starting
           and ending balances (starting auto-filled from the previous
           reconciliation), with the derived "Change in balance" shown live,
           plus an "Automatically select" control for pre-ticking.
  Page 2 — check-off. Every not-yet-reconciled row with a tick column and
           Withdrawal/Deposit amounts; running WITHDRAWALS / DEPOSITS
           subtotals and a live **Missing** figure (= change in balance −
           net of the ticked rows). When Missing hits zero the statement
           ties out. "Add Transaction" enters a missing line without leaving
           the screen.

Close model (amendment): a single **Save** always closes the statement —
clean when Missing is zero, otherwise closed-but-flagged (the history list
shows it as out of balance). Resume is offered on Cancel ("Save & finish
later"). The ``statement`` row is materialised only at Save / finish-later,
so an abandoned NEW pass leaves nothing behind.

Modes (driven by what the caller passes as ``statement``):
  - NEW    — ``statement=None``; starts on page 1.
  - RESUME — an ``'open'`` statement; starts on page 2 with ticks pre-loaded.
  - VIEW   — a ``'reconciled'`` statement; page 2 read-only with a "Reopen
             for editing" verb that transitions the dialog to RESUME.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import (
    AccountSummary,
    Repository,
    StatementRow,
)
from mfl_desktop import txn_status
from mfl_desktop.ui.date_widgets import make_date_edit
from mfl_desktop.ui.transaction_dialog import NewTransactionDialog


_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€"}

# Tailwind slate / accent hexes, consistent with the rest of the UI.
_MUTED = "#475569"
_FAINT = "#94A3B8"
_INK = "#0F172A"
_GREEN = "#16A34A"
_RED = "#DC2626"

# Row data roles.
_ROLE_TXN_ID = Qt.UserRole
_ROLE_AMOUNT = Qt.UserRole + 1


def _fmt(amount: Decimal, currency: str) -> str:
    """`-£40.00` / `$1,250.00` — minus sits outside the symbol, matching the
    rest of the app."""
    sym = _SYMBOL.get(currency, f"{currency} ")
    if amount < 0:
        return f"-{sym}{(-amount):,.2f}"
    return f"{sym}{amount:,.2f}"


def _parse_amount(text: str) -> Optional[Decimal]:
    """Parse a user-typed balance, tolerating currency symbols, commas, and
    a leading minus. Returns None if blank/unparseable."""
    t = (text or "").strip()
    for s in ("£", "$", "€", ","):
        t = t.replace(s, "")
    t = t.strip()
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _iso_to_qdate(iso: str) -> QDate:
    d = date.fromisoformat(iso)
    return QDate(d.year, d.month, d.day)


def _qdate_to_iso(qd: QDate) -> str:
    return date(qd.year(), qd.month(), qd.day()).isoformat()


class ReconcileWizard(QDialog):
    """Two-page reconciliation dialog. See module docstring for the modes.

    After the dialog closes, the caller should refresh its statement list
    regardless of result — :pyattr:`committed` reports whether anything was
    persisted (close / finish-later / reopen)."""

    def __init__(
        self,
        *,
        repo: Repository,
        account: AccountSummary,
        statement: Optional[StatementRow] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._account = account
        self._ccy = account.currency
        self._statement = statement
        self.committed = False

        if statement is None:
            self._mode = "new"
        elif statement.status == "open":
            self._mode = "resume"
        else:
            self._mode = "view"

        self.setWindowTitle(f"Reconcile — {account.name}")
        self.setModal(True)
        self.setMinimumSize(720, 560)

        self._recompute_guard = False
        self._auto_selected_once = False
        self._baseline_ticks: set[int] = set()
        # Ending-balance autofill state. The ending balance defaults to the
        # account's recorded balance *as of the end date* and re-derives when
        # the end date changes — unless the user types their own figure (taken
        # from a paper statement), after which we stop overwriting it. The
        # suppress flag silences the date-change handler during programmatic
        # seed/load so it doesn't fire mid-population.
        self._end_balance_user_set = False
        self._suppress_end_date_autofill = False

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._build_balances_page())
        self._stack.addWidget(self._build_checkoff_page())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._stack)

        self._seed_defaults()
        if self._mode == "new":
            self._stack.setCurrentIndex(0)
        else:
            self._load_statement_into_pages()
            self._enter_checkoff(auto_select=False)
            self._stack.setCurrentIndex(1)
        self._apply_mode_to_checkoff()

    # ── Page 1: balances ────────────────────────────────────────────────

    def _build_balances_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(14)

        title = QLabel(
            "Enter the dates and opening and closing amounts for the statement."
        )
        title.setStyleSheet(f"font-weight: 600; color: {_INK};")
        title.setWordWrap(True)
        layout.addWidget(title)

        cols = QHBoxLayout()
        cols.setSpacing(40)

        # Left: dates.
        self._start_date = make_date_edit()
        self._end_date = make_date_edit(maximum_today=True)
        dates_form = QFormLayout()
        dates_form.addRow("Starting date:", self._start_date)
        dates_form.addRow("Ending date:", self._end_date)
        cols.addLayout(dates_form)

        # Right: balances + derived change.
        self._start_balance = QLineEdit()
        self._start_balance.setAlignment(Qt.AlignRight)
        self._end_balance = QLineEdit()
        self._end_balance.setAlignment(Qt.AlignRight)
        self._change_label = QLabel("—")
        self._change_label.setAlignment(Qt.AlignRight)
        self._change_label.setStyleSheet(f"color: {_MUTED};")
        bal_form = QFormLayout()
        bal_form.addRow("Starting balance:", self._start_balance)
        bal_form.addRow("Ending balance:", self._end_balance)
        bal_form.addRow("Change in balance:", self._change_label)
        cols.addLayout(bal_form)

        layout.addLayout(cols)

        self._start_balance.textChanged.connect(self._update_change_label)
        self._end_balance.textChanged.connect(self._update_change_label)
        # textEdited fires only on user keystrokes (not setText), so it marks a
        # deliberate override; dateChanged re-derives the ending balance for the
        # new statement end date.
        self._end_balance.textEdited.connect(self._on_end_balance_edited)
        self._end_date.dateChanged.connect(self._on_end_date_changed)

        # Automatically select.
        auto_row = QHBoxLayout()
        auto_row.addWidget(QLabel("Automatically select:"))
        self._auto_combo = QComboBox()
        self._auto_combo.addItem("Matched Transactions", userData="matched")
        self._auto_combo.addItem("Nothing", userData="none")
        auto_row.addWidget(self._auto_combo)
        auto_row.addStretch(1)
        layout.addLayout(auto_row)

        # Confidence gate (ADR-130): by default only 'matched' (download-
        # confirmed) rows are eligible. Some banks offer no download, so allow
        # reconciling off eyeballed 'cleared' rows too when the user opts in.
        self._include_cleared_check = QCheckBox(
            "Include cleared transactions (seen at the bank, not yet downloaded)"
        )
        self._include_cleared_check.setToolTip(
            "Off: only matched (download-confirmed) transactions can be ticked.\n"
            "On: also allow 'cleared' rows you've seen post but not downloaded —\n"
            "for accounts with no statement download."
        )
        self._include_cleared_check.toggled.connect(
            self._on_include_cleared_toggled
        )
        layout.addWidget(self._include_cleared_check)

        layout.addStretch(1)

        # Buttons.
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._p1_cancel = QPushButton("Cancel")
        self._p1_cancel.clicked.connect(self._on_cancel)
        self._p1_next = QPushButton("Next")
        self._p1_next.setDefault(True)
        self._p1_next.clicked.connect(self._on_next)
        btn_row.addWidget(self._p1_cancel)
        btn_row.addWidget(self._p1_next)
        layout.addLayout(btn_row)

        return page

    def _seed_defaults(self) -> None:
        """Pre-fill page 1 for a NEW statement: starting balance/date from the
        last reconciliation, ending date = today, and ending balance = the
        account's recorded balance *as of the ending date*.

        The ending balance used to be the account's *current* recorded balance
        (`compute_account_balances`), which was wrong for any statement that
        closed before today — it suggested today's balance regardless of the
        end date. It now tracks the end date via `_derive_ending_balance`."""
        statements = self._repo.list_statements_for_account(self._account.id)
        last_reconciled = next(
            (s for s in statements if s.status == "reconciled"), None
        )
        today = date.today()
        if last_reconciled is not None:
            start_d = date.fromisoformat(last_reconciled.end_date) + timedelta(days=1)
            start_bal = last_reconciled.ending_balance
        else:
            start_d = today.replace(day=1)
            start_bal = self._account.opening_balance

        self._suppress_end_date_autofill = True
        self._start_date.setDate(QDate(start_d.year, start_d.month, start_d.day))
        self._end_date.setDate(QDate.currentDate())
        self._start_balance.setText(f"{start_bal:.2f}")
        self._suppress_end_date_autofill = False
        self._derive_ending_balance()

    def _load_statement_into_pages(self) -> None:
        """Populate page 1 from an existing (open/reconciled) statement. The
        stored ending balance is authoritative, so it is marked user-set —
        editing the end date afterwards won't silently overwrite it."""
        s = self._statement
        assert s is not None
        self._suppress_end_date_autofill = True
        self._start_date.setDate(_iso_to_qdate(s.start_date))
        self._end_date.setDate(_iso_to_qdate(s.end_date))
        self._start_balance.setText(f"{s.starting_balance:.2f}")
        self._end_balance.setText(f"{s.ending_balance:.2f}")
        self._suppress_end_date_autofill = False
        self._end_balance_user_set = True
        self._update_change_label()

    def _derive_ending_balance(self) -> None:
        """Set the ending balance to the account's recorded balance as of the
        statement end date (inclusive). `setText` does not emit `textEdited`,
        so this never trips the user-override guard."""
        end_iso = _qdate_to_iso(self._end_date.date())
        bal = self._repo.balance_as_of(self._account.id, end_iso)
        self._end_balance.setText(f"{bal:.2f}")
        self._update_change_label()

    def _on_end_balance_edited(self, _text: str) -> None:
        """The user typed their own ending balance (e.g. the figure from a
        paper statement) — stop auto-deriving it from the end date so we don't
        clobber it."""
        self._end_balance_user_set = True

    def _on_end_date_changed(self, _qd: QDate) -> None:
        """Re-derive the suggested ending balance for the new end date. Skipped
        during programmatic seed/load, in read-only VIEW mode, and once the
        user has typed their own ending balance."""
        if self._suppress_end_date_autofill:
            return
        if self._mode == "view" or self._end_balance_user_set:
            return
        self._derive_ending_balance()

    def _page1_change(self) -> Optional[Decimal]:
        start = _parse_amount(self._start_balance.text())
        end = _parse_amount(self._end_balance.text())
        if start is None or end is None:
            return None
        return end - start

    def _update_change_label(self) -> None:
        change = self._page1_change()
        if change is None:
            self._change_label.setText("—")
        else:
            self._change_label.setText(_fmt(change, self._ccy))

    def _validate_page1(self) -> bool:
        if _qdate_to_iso(self._start_date.date()) > _qdate_to_iso(self._end_date.date()):
            QMessageBox.warning(
                self, "Invalid dates",
                "The starting date must be on or before the ending date.",
            )
            return False
        if _parse_amount(self._start_balance.text()) is None:
            QMessageBox.warning(
                self, "Starting balance",
                "Enter a numeric starting balance.",
            )
            return False
        if _parse_amount(self._end_balance.text()) is None:
            QMessageBox.warning(
                self, "Ending balance",
                "Enter a numeric ending balance.",
            )
            return False
        return True

    def _on_next(self) -> None:
        if not self._validate_page1():
            return
        first_time = not self._auto_selected_once
        self._enter_checkoff(auto_select=(self._mode == "new" and first_time))
        self._stack.setCurrentIndex(1)

    # ── Page 2: check-off ───────────────────────────────────────────────

    def _build_checkoff_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(10)

        self._checkoff_title = QLabel(
            "Check off the transactions that appear on your statement until "
            "the Missing value is zero."
        )
        self._checkoff_title.setStyleSheet(f"font-weight: 600; color: {_INK};")
        self._checkoff_title.setWordWrap(True)
        layout.addWidget(self._checkoff_title)

        # Summary strip: left subtotals · middle balances · right Missing.
        strip = QHBoxLayout()
        strip.setSpacing(24)

        sub_box = QVBoxLayout()
        sub_box.setSpacing(2)
        self._withdrawals_label = QLabel("WITHDRAWALS  —")
        self._deposits_label = QLabel("DEPOSITS  —")
        for lbl in (self._withdrawals_label, self._deposits_label):
            lbl.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        sub_box.addWidget(self._withdrawals_label)
        sub_box.addWidget(self._deposits_label)
        strip.addLayout(sub_box)

        strip.addStretch(1)

        bal_box = QVBoxLayout()
        bal_box.setSpacing(2)
        self._strip_change = QLabel("—")
        self._strip_change.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        self._edit_btn = QPushButton("Edit dates / balances…")
        self._edit_btn.setFlat(True)
        self._edit_btn.setStyleSheet("text-align: right;")
        self._edit_btn.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        bal_box.addWidget(self._strip_change)
        bal_box.addWidget(self._edit_btn)
        strip.addLayout(bal_box)

        missing_box = QVBoxLayout()
        missing_box.setSpacing(0)
        cap = QLabel("MISSING")
        cap.setStyleSheet(f"color: {_FAINT}; font-size: 10px;")
        cap.setAlignment(Qt.AlignRight)
        self._missing_label = QLabel("—")
        self._missing_label.setAlignment(Qt.AlignRight)
        self._missing_label.setStyleSheet(
            f"font-size: 18px; font-weight: 700; color: {_GREEN};"
        )
        missing_box.addWidget(cap)
        missing_box.addWidget(self._missing_label)
        strip.addLayout(missing_box)

        layout.addLayout(strip)

        # Search.
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search payee / amount / date…")
        self._search.textChanged.connect(self._apply_search)
        layout.addWidget(self._search)

        # Confidence warning (ADR-130): cleared-but-not-downloaded rows in the
        # period that aren't shown because they're not eligible. Hidden unless
        # there are any and 'include cleared' is off.
        self._cleared_warning = QLabel("")
        self._cleared_warning.setWordWrap(True)
        self._cleared_warning.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        self._cleared_warning.setVisible(False)
        layout.addWidget(self._cleared_warning)

        # Table.
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["", "Date", "Payee", "Withdrawal", "Deposit"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self._table, 1)

        # Buttons: Add Transaction (left) · Cancel / Save (right).
        btn_row = QHBoxLayout()
        self._add_txn_btn = QPushButton("Add Transaction")
        self._add_txn_btn.clicked.connect(self._on_add_transaction)
        btn_row.addWidget(self._add_txn_btn)
        btn_row.addStretch(1)
        self._p2_cancel = QPushButton("Cancel")
        self._p2_cancel.clicked.connect(self._on_cancel)
        self._reopen_btn = QPushButton("Reopen for editing")
        self._reopen_btn.clicked.connect(self._on_reopen)
        self._save_btn = QPushButton("Save")
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._p2_cancel)
        btn_row.addWidget(self._reopen_btn)
        btn_row.addWidget(self._save_btn)
        layout.addLayout(btn_row)

        return page

    def _enter_checkoff(self, *, auto_select: bool) -> None:
        """(Re)build the check-off table. Preserves current ticks across an
        Edit round-trip; runs auto-select only on first NEW entry."""
        prior_ticks = self._collect_ticked_ids() if self._table.rowCount() else None

        # Always show every eligible row (any date), plus this statement's own
        # rows when viewing/resuming a closed one so they aren't hidden.
        sid = self._statement.id if self._statement is not None else None
        include_cleared = self._include_cleared_check.isChecked()
        rows = self._repo.list_reconcilable_txns(
            self._account.id, include_statement_id=sid,
            include_cleared=include_cleared,
        )

        if auto_select:
            mode = self._auto_combo.currentData()
            if mode == "matched":
                # Auto-tick the bank-confirmed rows that fall WITHIN the
                # statement period — matched always, plus cleared when the user
                # opted to include them (ADR-130). Rows outside the dates stay
                # visible but deselected.
                start_iso = _qdate_to_iso(self._start_date.date())
                end_iso = _qdate_to_iso(self._end_date.date())
                eligible = {txn_status.MATCHED}
                if include_cleared:
                    eligible.add(txn_status.CLEARED)
                preset = {
                    txn.id for txn in rows
                    if txn.status in eligible
                    and start_iso <= txn.posted_date <= end_iso
                }
            else:
                preset = set()
            self._auto_selected_once = True
        elif prior_ticks is not None:
            preset = prior_ticks
        elif self._statement is not None:
            preset = self._repo.get_statement_tick_ids(self._statement.id)
        else:
            preset = set()

        self._recompute_guard = True
        self._table.setRowCount(0)
        for txn in rows:
            self._append_row(
                txn_id=txn.id,
                posted_date=txn.posted_date,
                payee=txn.payee_name or "(no payee)",
                amount=txn.amount,
                ticked=(txn.id in preset),
            )
        self._recompute_guard = False

        if self._mode != "view" and prior_ticks is None:
            # First entry establishes the dirty baseline.
            self._baseline_ticks = set(preset)

        self._strip_change.setText(
            "Change in balance: "
            + (self._fmt_change_strip())
        )
        self._update_cleared_warning()
        self._recompute()

    def _update_cleared_warning(self) -> None:
        """Surface any ``cleared`` (seen-but-not-downloaded) rows in the period
        that the confidence gate is excluding, with a nudge to include them
        (ADR-130). Hidden when cleared are already included or there are none."""
        if self._include_cleared_check.isChecked():
            self._cleared_warning.setVisible(False)
            return
        start_iso = _qdate_to_iso(self._start_date.date())
        end_iso = _qdate_to_iso(self._end_date.date())
        n = self._repo.count_cleared_in_period(
            self._account.id, start_iso, end_iso,
        )
        if n <= 0:
            self._cleared_warning.setVisible(False)
            return
        it = "it" if n == 1 else "them"
        self._cleared_warning.setText(
            f"⚠ {n} cleared transaction{'' if n == 1 else 's'} in this period "
            f"{'is' if n == 1 else 'are'} not shown — you saw {it} post but no "
            f"download has confirmed {it}. Tick “Include cleared…” on the "
            f"balances page to reconcile against {it}."
        )
        self._cleared_warning.setVisible(True)

    def _on_include_cleared_toggled(self, _checked: bool) -> None:
        """Re-gate the candidate set live when already on the check-off page.
        Preserves current ticks; newly-eligible cleared rows appear unticked
        (set the toggle on the balances page *before* Next to auto-select them)."""
        if self._mode == "view" or not self._auto_selected_once:
            return  # check-off page not built yet; the next Next reads the box
        self._enter_checkoff(auto_select=False)

    def _fmt_change_strip(self) -> str:
        change = self._statement_change()
        return _fmt(change, self._ccy) if change is not None else "—"

    def _append_row(
        self, *, txn_id: int, posted_date: str, payee: str,
        amount: Decimal, ticked: bool,
    ) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)

        tick = QTableWidgetItem()
        tick.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        tick.setCheckState(Qt.Checked if ticked else Qt.Unchecked)
        tick.setData(_ROLE_TXN_ID, txn_id)
        tick.setData(_ROLE_AMOUNT, str(amount))  # Decimal not QVariant-safe
        self._table.setItem(r, 0, tick)

        self._table.setItem(r, 1, QTableWidgetItem(posted_date))
        self._table.setItem(r, 2, QTableWidgetItem(payee))

        withdrawal = QTableWidgetItem(
            f"{(-amount):,.2f}" if amount < 0 else ""
        )
        withdrawal.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._table.setItem(r, 3, withdrawal)

        deposit = QTableWidgetItem(
            f"{amount:,.2f}" if amount > 0 else ""
        )
        deposit.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._table.setItem(r, 4, deposit)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._recompute_guard:
            return
        if item.column() == 0:
            self._recompute()

    def _row_amount(self, row: int) -> Decimal:
        return Decimal(self._table.item(row, 0).data(_ROLE_AMOUNT))

    def _row_ticked(self, row: int) -> bool:
        return self._table.item(row, 0).checkState() == Qt.Checked

    def _collect_ticked_ids(self) -> set[int]:
        ids: set[int] = set()
        for r in range(self._table.rowCount()):
            if self._row_ticked(r):
                ids.add(int(self._table.item(r, 0).data(_ROLE_TXN_ID)))
        return ids

    def _statement_change(self) -> Optional[Decimal]:
        return self._page1_change()

    def _recompute(self) -> None:
        """Recompute subtotals + Missing over ALL rows (filtered-out rows that
        are ticked still count, matching Banktivity)."""
        withdrawals = Decimal("0.00")
        deposits = Decimal("0.00")
        for r in range(self._table.rowCount()):
            if not self._row_ticked(r):
                continue
            amt = self._row_amount(r)
            if amt < 0:
                withdrawals += -amt
            else:
                deposits += amt
        net = deposits - withdrawals

        self._withdrawals_label.setText(
            f"WITHDRAWALS  {_fmt(withdrawals, self._ccy)}"
        )
        self._deposits_label.setText(
            f"DEPOSITS  {_fmt(deposits, self._ccy)}"
        )

        change = self._statement_change()
        if change is None:
            self._missing_label.setText("—")
            self._missing_label.setStyleSheet(
                f"font-size: 18px; font-weight: 700; color: {_RED};"
            )
            return
        missing = change - net
        if missing == 0:
            self._missing_label.setText(f"✓ {_fmt(Decimal('0.00'), self._ccy)}")
            colour = _GREEN
        else:
            self._missing_label.setText(_fmt(missing, self._ccy))
            colour = _RED
        self._missing_label.setStyleSheet(
            f"font-size: 18px; font-weight: 700; color: {colour};"
        )

    def _apply_search(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for r in range(self._table.rowCount()):
            if not needle:
                self._table.setRowHidden(r, False)
                continue
            hay = " ".join(
                self._table.item(r, c).text().lower() for c in (1, 2, 3, 4)
            )
            self._table.setRowHidden(r, needle not in hay)

    # ── Mode application ────────────────────────────────────────────────

    def _apply_mode_to_checkoff(self) -> None:
        read_only = self._mode == "view"
        # The confidence gate can't change a closed statement's candidate set.
        self._include_cleared_check.setEnabled(not read_only)
        self._add_txn_btn.setVisible(not read_only)
        self._edit_btn.setVisible(not read_only)
        self._save_btn.setVisible(not read_only)
        self._reopen_btn.setVisible(read_only)
        if read_only:
            self._checkoff_title.setText(
                "Reconciled statement (read-only). Reopen to edit."
            )
            # Lock the tick column.
            self._recompute_guard = True
            for r in range(self._table.rowCount()):
                self._table.item(r, 0).setFlags(Qt.ItemIsEnabled)
            self._recompute_guard = False
        else:
            self._checkoff_title.setText(
                "Check off the transactions that appear on your statement "
                "until the Missing value is zero."
            )

    # ── Add Transaction ─────────────────────────────────────────────────

    def _on_add_transaction(self) -> None:
        dialog = NewTransactionDialog(
            accounts=[self._account],
            categories=self._repo.list_categories_flat(),
            default_account_id=self._account.id,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        if values is None:
            return
        payee_id = self._repo.get_or_create_payee(values.payee_name)
        new_id = self._repo.insert_transaction(
            account_id=self._account.id,
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
        self._recompute_guard = True
        self._append_row(
            txn_id=new_id,
            posted_date=values.posted_date,
            payee=values.payee_name or "(no payee)",
            amount=values.amount,
            ticked=True,
        )
        self._recompute_guard = False
        self._recompute()

    # ── Save / Cancel / Reopen ──────────────────────────────────────────

    def _gather_page1(self) -> tuple[str, str, Decimal, Decimal]:
        return (
            _qdate_to_iso(self._start_date.date()),
            _qdate_to_iso(self._end_date.date()),
            _parse_amount(self._start_balance.text()),
            _parse_amount(self._end_balance.text()),
        )

    def _on_save(self) -> None:
        if not self._validate_page1():
            self._stack.setCurrentIndex(0)
            return
        ticked = sorted(self._collect_ticked_ids())
        change = self._statement_change()
        # Missing for the confirm copy.
        missing = None
        if change is not None:
            net_sum = Decimal("0.00")
            for r in range(self._table.rowCount()):
                if self._row_ticked(r):
                    net_sum += self._row_amount(r)
            missing = change - net_sum
        if missing is not None and missing != 0:
            resp = QMessageBox.question(
                self, "Out of balance",
                f"This statement is out of balance by "
                f"{_fmt(abs(missing), self._ccy)}.\n\n"
                "It will be saved and flagged as out of balance on the "
                "statement history. You can reopen it later to fix.\n\n"
                "Save anyway?",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if resp != QMessageBox.Save:
                return

        start_iso, end_iso, start_bal, end_bal = self._gather_page1()
        try:
            if self._mode == "new":
                stmt = self._repo.create_statement(
                    account_id=self._account.id,
                    start_date=start_iso, end_date=end_iso,
                    starting_balance=start_bal, ending_balance=end_bal,
                )
                self._statement = stmt
            else:
                # Resume: persist any page-1 edits before closing.
                self._statement = self._repo.update_statement(
                    self._statement.id,
                    start_date=start_iso, end_date=end_iso,
                    starting_balance=start_bal, ending_balance=end_bal,
                )
            self._repo.close_statement(
                self._statement.id, ticked_ids=ticked,
            )
        except ValueError as e:
            QMessageBox.warning(self, "Could not save", str(e))
            return
        self.committed = True
        self.accept()

    def _on_cancel(self) -> None:
        if self._mode == "view" or not self._is_dirty():
            self.reject()
            return
        resp = QMessageBox.question(
            self, "Finish later?",
            "Keep this reconciliation to finish later, or discard it?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if resp == QMessageBox.Cancel:
            return
        if resp == QMessageBox.Discard:
            self.reject()
            return
        # Save & finish later → persist as an open statement.
        if not self._validate_page1():
            self._stack.setCurrentIndex(0)
            return
        start_iso, end_iso, start_bal, end_bal = self._gather_page1()
        ticked = sorted(self._collect_ticked_ids())
        try:
            if self._mode == "new":
                stmt = self._repo.create_statement(
                    account_id=self._account.id,
                    start_date=start_iso, end_date=end_iso,
                    starting_balance=start_bal, ending_balance=end_bal,
                )
                self._statement = stmt
            else:
                self._statement = self._repo.update_statement(
                    self._statement.id,
                    start_date=start_iso, end_date=end_iso,
                    starting_balance=start_bal, ending_balance=end_bal,
                )
            self._repo.set_statement_ticks(self._statement.id, ticked)
        except ValueError as e:
            QMessageBox.warning(self, "Could not save", str(e))
            return
        self.committed = True
        self.accept()

    def _on_reopen(self) -> None:
        assert self._statement is not None
        try:
            self._statement = self._repo.reopen_statement(self._statement.id)
        except ValueError as e:
            QMessageBox.warning(self, "Could not reopen", str(e))
            return
        self.committed = True
        self._mode = "resume"
        # Rebuild the table: the statement's rows just reverted to Cleared and
        # must reappear as editable + pre-ticked from the preserved tick set.
        # (A plain unlock-in-place left the table showing the pre-reopen rows,
        # which is why the rows only appeared after a cancel + reopen.)
        self._enter_checkoff(auto_select=False)
        self._apply_mode_to_checkoff()
        self._baseline_ticks = self._collect_ticked_ids()

    def _is_dirty(self) -> bool:
        if self._mode == "new" and not self._auto_selected_once:
            # Never advanced past page 1 — nothing worth keeping.
            return False
        return self._collect_ticked_ids() != self._baseline_ticks
