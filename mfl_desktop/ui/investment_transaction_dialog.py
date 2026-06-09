"""Investment transaction dialog — create + edit (ADR-048).

The single edit surface for investment-account transactions — opened by
**New Transaction** on an investment account (create) and by **double-clicking**
an investment row (edit). Deliberately investment-shaped, not the cash form:
the fields are **Date · Action · Symbol · Security · Qty · Price · Status ·
Memo** (no Payee / Commission / standing cash-amount box). The visible fields
adapt to the action so you only ever see what's relevant.

Field flow:
  * **Symbol drives Security.** Type a ticker and the Security name fills in —
    from an existing security with that symbol, or (when online with a Tiingo
    key) from a Tiingo metadata lookup. Picking an existing Security fills the
    Symbol the other way. A typed name with no match creates a new security.
  * **Buy / Sell** — Qty + Price; the signed cash impact is computed
    (`∓ qty·price`) silently. **Reinvested dividend / Shares in-out** — Qty
    (+ optional Price for basis); cash impact 0. **Dividend / Interest /
    Cap-gain** — an Amount-in field replaces Qty/Price. **Cash in/out** — an
    Amount field only, no security.

The stored ``txn.amount`` is always the SIGNED CASH IMPACT, so cash balance =
SUM(amount) holds (ADR-043). When editing a trade whose Qty/Price are
unchanged, the original stored amount is preserved (so a re-save never drifts a
penny off the imported figure). The dialog writes through the Repository and
calls ``accept()``; the caller reloads. Transfer actions (XIn/XOut) and stock
splits are out of scope for v1.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, Repository, TransactionRow
from mfl_desktop.import_engine.qif_actions import is_income, is_reinvest
from mfl_desktop.prices import lookup_symbol_name

# Curated action list for manual entry (label, canonical QIF action stored in
# txn.action — must match the strings the holdings/returns engines classify via
# qif_actions). Transfer (XIn/XOut) + StkSplit deliberately omitted in v1.
_ACTIONS: list[tuple[str, str]] = [
    ("Buy", "Buy"),
    ("Sell", "Sell"),
    ("Dividend (cash)", "Div"),
    ("Reinvested dividend", "ReinvDiv"),
    ("Interest", "IntInc"),
    ("Long-term cap-gain dist.", "CGLong"),
    ("Short-term cap-gain dist.", "CGShort"),
    ("Shares in (transfer)", "ShrsIn"),
    ("Shares out (transfer)", "ShrsOut"),
    ("Cash in / out", "Cash"),
]

_STATUSES = ("Pending", "Uncleared", "Cleared", "Reconciled")

# The system income category an income/reinvest action routes to — mirrors
# qif_parser._INCOME_CATEGORY ("Income:Investment income").
_INCOME_PATH = ["Income", "Investment income"]


def _kind(action: str) -> str:
    """Classify an action into the UI behaviour group."""
    a = (action or "").strip().lower()
    if a == "buy":
        return "buy"
    if a == "sell":
        return "sell"
    if a == "reinvdiv":
        return "reinvest"
    if a in ("shrsin", "shrsout"):
        return "shares"
    if a == "cash":
        return "cash"
    return "income"          # div / intinc / cglong / cgshort


class InvestmentTransactionDialog(QDialog):
    """Create or edit one investment-account transaction."""

    def __init__(
        self, repo: Repository, account: AccountSummary,
        seed: Optional[TransactionRow] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._account = account
        self._seed = seed                 # None = create mode
        self._loading = True              # suppress amount auto-calc while populating
        self._last_lookup_symbol = ""     # avoid repeat online lookups
        self.setWindowTitle(
            "Edit investment transaction" if seed else "New investment transaction"
        )
        self.setMinimumWidth(460)

        outer = QVBoxLayout(self)
        self._form = QFormLayout()
        outer.addLayout(self._form)

        acct_label = QLabel(f"{account.name}  ·  {account.currency}")
        acct_label.setStyleSheet("QLabel { color: #475569; }")
        self._form.addRow("Account:", acct_label)

        self._date = QDateEdit()
        self._date.setCalendarPopup(True)
        self._date.setDisplayFormat("yyyy-MM-dd")
        self._date.setDate(QDate.currentDate())
        self._form.addRow("Date:", self._date)

        self._action = QComboBox()
        for label, value in _ACTIONS:
            self._action.addItem(label, value)
        self._action.currentIndexChanged.connect(self._on_action_changed)
        self._form.addRow("Action:", self._action)

        # Symbol drives Security (typed ticker → name lookup).
        self._symbol = QLineEdit()
        self._symbol.setPlaceholderText("ticker, e.g. TSLA (blank = untickered)")
        self._symbol.editingFinished.connect(self._on_symbol_finished)
        self._form.addRow("Symbol:", self._symbol)

        self._security = QComboBox()
        self._security.setEditable(True)
        self._security.setInsertPolicy(QComboBox.NoInsert)
        self._security.completer().setCompletionMode(
            self._security.completer().CompletionMode.PopupCompletion
        )
        self._security.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self._security.addItem("", None)   # blank first entry
        self._symbol_by_sid: dict[int, str] = {}
        for s in self._repo.list_securities():
            self._security.addItem(s.name, s.id)
            self._symbol_by_sid[s.id] = s.symbol or ""
        self._security.setEditText("")
        self._security.currentIndexChanged.connect(self._on_security_changed)
        self._form.addRow("Security:", self._security)

        self._qty = QLineEdit()
        self._qty.setPlaceholderText("shares")
        self._qty.textChanged.connect(self._recompute_hint)
        self._form.addRow("Quantity:", self._qty)

        self._price = QLineEdit()
        self._price.setPlaceholderText("per share")
        self._price.textChanged.connect(self._recompute_hint)
        self._form.addRow("Price:", self._price)

        self._amount = QLineEdit()
        self._amount.setPlaceholderText("cash amount")
        self._form.addRow("Amount:", self._amount)

        self._status = QComboBox()
        self._status.addItems(_STATUSES)
        self._status.setCurrentText("Cleared")
        self._form.addRow("Status:", self._status)

        self._memo = QLineEdit()
        self._form.addRow("Memo:", self._memo)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("QLabel { color: #64748B; font-size: 11px; }")
        outer.addWidget(self._hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setDefault(True)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        if seed is not None:
            self._populate_from_seed(seed)
        self._loading = False
        self._apply_action_rules()

    # ── row visibility ──

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        field.setVisible(visible)
        label = self._form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    # ── population ──

    def _populate_from_seed(self, seed: TransactionRow) -> None:
        if seed.action:
            i = self._action.findData(seed.action)
            if i < 0:
                # An imported action we don't list (e.g. CvrShrt) — add it so
                # the edit is faithful rather than silently remapped.
                self._action.addItem(seed.action, seed.action)
                i = self._action.findData(seed.action)
            self._action.setCurrentIndex(i)
        if seed.security_id is not None:
            si = self._security.findData(seed.security_id)
            if si >= 0:
                self._security.setCurrentIndex(si)
        self._symbol.setText(seed.security_symbol or "")
        self._last_lookup_symbol = (seed.security_symbol or "").strip().upper()
        try:
            self._date.setDate(QDate.fromString(seed.posted_date, "yyyy-MM-dd"))
        except Exception:
            pass
        if seed.quantity is not None:
            self._qty.setText(_trim(seed.quantity))
        if seed.price is not None:
            self._price.setText(_trim(seed.price))
        self._amount.setText(f"{seed.amount:.2f}")
        self._memo.setText(seed.memo or "")
        self._status.setCurrentText(seed.status or "Cleared")

    # ── action-driven field rules ──

    def _on_action_changed(self, _idx: int) -> None:
        self._apply_action_rules()

    def _apply_action_rules(self) -> None:
        kind = _kind(self._current_action())
        show_sec = kind != "cash"
        show_qty = kind in ("buy", "sell", "reinvest", "shares")
        show_price = kind in ("buy", "sell", "reinvest", "shares")
        show_amount = kind in ("income", "cash")

        self._set_row_visible(self._symbol, show_sec)
        self._set_row_visible(self._security, show_sec)
        self._set_row_visible(self._qty, show_qty)
        self._set_row_visible(self._price, show_price)
        self._set_row_visible(self._amount, show_amount)
        self._recompute_hint()

    def _current_action(self) -> str:
        return self._action.currentData() or ""

    def _recompute_hint(self, *_args) -> None:
        kind = _kind(self._current_action())
        if kind == "buy":
            base = "Cash impact = −(quantity × price), computed for you."
        elif kind == "sell":
            base = "Cash impact = +(quantity × price), computed for you."
        elif kind == "reinvest":
            base = "Reinvested dividend — no cash moves; counts as income."
        elif kind == "shares":
            base = "Share transfer — no cash moves. Price is optional (cost basis)."
        elif kind == "income":
            base = "Enter the cash received (positive)."
        else:  # cash
            base = "Enter the signed cash amount (− for money out)."
        if kind in ("buy", "sell"):
            qty = _to_decimal(self._qty.text())
            price = _to_decimal(self._price.text())
            if qty is not None and price is not None:
                gross = qty * price
                signed = -gross if kind == "buy" else gross
                base += f"  →  {signed:,.2f}"
        if kind != "cash":
            base += "  ·  Type a ticker to auto-fill the security name (online)."
        self._hint.setText(base)

    # ── symbol ⇄ security ──

    def _on_security_changed(self, idx: int) -> None:
        """Selecting an existing security mirrors its stored ticker into Symbol."""
        sid = self._security.itemData(idx)
        if sid is not None:
            sym = self._symbol_by_sid.get(int(sid), "")
            self._symbol.setText(sym)
            self._last_lookup_symbol = sym.strip().upper()

    def _on_symbol_finished(self) -> None:
        """Resolve the typed ticker to a Security: an existing security with
        that symbol, else (online) a Tiingo name lookup. Silent fallback to
        manual entry when offline / unknown."""
        sym = self._symbol.text().strip().upper()
        if not sym or sym == self._last_lookup_symbol:
            return
        self._last_lookup_symbol = sym
        # 1) existing security carrying this ticker?
        for sid, s_sym in self._symbol_by_sid.items():
            if (s_sym or "").strip().upper() == sym:
                i = self._security.findData(sid)
                if i >= 0:
                    self._security.setCurrentIndex(i)
                return
        # 2) don't clobber a security the user has already chosen/typed.
        if self._security.currentText().strip():
            return
        # 3) online lookup → fill the name as a (new) security.
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            name = lookup_symbol_name(self._repo, sym)
        finally:
            QApplication.restoreOverrideCursor()
        if name:
            self._security.setCurrentIndex(-1)
            self._security.setEditText(name)

    # ── save ──

    def _on_save(self) -> None:
        action = self._current_action()
        kind = _kind(action)

        # Security (+ its ticker, which lives on the security master).
        security_id: Optional[int] = None
        if kind != "cash":
            sid, name = self._resolve_security()
            if sid is None and not name:
                QMessageBox.warning(
                    self, "Save transaction",
                    "Pick or type a security for this action.",
                )
                return
            typed_symbol = self._symbol.text().strip()
            if sid is not None:
                security_id = sid
                if typed_symbol != (self._symbol_by_sid.get(sid, "") or ""):
                    try:
                        self._repo.update_security(sid, symbol=typed_symbol)
                    except ValueError as e:
                        QMessageBox.warning(self, "Save transaction", str(e))
                        return
            else:
                security_id = self._repo.get_or_create_security(name, typed_symbol)

        # Quantity / price (only relevant to share-moving actions).
        wants_qty = kind in ("buy", "sell", "reinvest", "shares")
        qty = _to_decimal(self._qty.text()) if wants_qty else None
        price = _to_decimal(self._price.text()) if wants_qty else None
        if kind in ("buy", "sell", "reinvest", "shares"):
            if qty is None or qty <= 0:
                QMessageBox.warning(self, "Save transaction", "Enter a positive quantity.")
                return
        if kind in ("buy", "sell", "reinvest"):
            if price is None or price <= 0:
                QMessageBox.warning(self, "Save transaction", "Enter a positive price.")
                return

        amount = self._compute_amount(kind, qty, price)
        if amount is None:
            QMessageBox.warning(self, "Save transaction", "Enter a cash amount.")
            return
        if kind == "income" and amount < 0:
            QMessageBox.warning(
                self, "Save transaction", "Income should be a positive amount.",
            )
            return

        category_id = (
            self._repo.find_or_create_category_path(_INCOME_PATH, source="user")
            if (is_income(action) or is_reinvest(action))
            else self._repo.uncategorised_id()
        )
        posted_date = self._date.date().toString("yyyy-MM-dd")
        status = self._status.currentText()
        memo = self._memo.text().strip()

        try:
            if self._seed is None:
                self._repo.insert_transaction(
                    account_id=self._account.id,
                    posted_date=posted_date,
                    amount=amount,
                    payee_id=None,
                    category_id=category_id,
                    status=status,
                    memo=memo,
                    import_hash=None,
                    import_batch_id=None,
                    action=action,
                    security_id=security_id,
                    quantity=qty,
                    price=price,
                    commission=None,
                )
                self._repo.commit()
            else:
                self._repo.update_investment_transaction(
                    self._seed.id,
                    posted_date=posted_date,
                    amount=amount,
                    payee_id=self._seed.payee_id,
                    category_id=category_id,
                    status=status,
                    memo=memo,
                    action=action,
                    security_id=security_id,
                    quantity=qty,
                    price=price,
                    commission=None,
                )
            if security_id is not None:
                self._repo.seed_prices_from_transactions(security_ids=[security_id])
        except Exception as e:  # noqa: BLE001
            self._repo.rollback()
            QMessageBox.critical(
                self, "Could not save transaction",
                f"The transaction was not saved:\n\n{e}",
            )
            return
        self.accept()

    def _compute_amount(
        self, kind: str, qty: Optional[Decimal], price: Optional[Decimal],
    ) -> Optional[Decimal]:
        """The signed cash impact for this row. Trades derive from qty × price;
        share transfers / reinvests are zero; income / cash are user-entered.
        When editing a trade whose qty + price are unchanged, the seed's stored
        amount is preserved so a re-save never drifts off the imported figure."""
        if kind in ("income", "cash"):
            return _to_decimal(self._amount.text())
        if kind in ("reinvest", "shares"):
            return Decimal("0.00")
        # buy / sell
        if qty is None or price is None:
            return None
        if (
            self._seed is not None
            and self._seed.action
            and _kind(self._seed.action) == kind
            and self._seed.quantity is not None
            and self._seed.price is not None
            and float(qty) == float(self._seed.quantity)
            and float(price) == float(self._seed.price)
        ):
            return self._seed.amount
        gross = qty * price
        return (-gross if kind == "buy" else gross).quantize(Decimal("0.01"))

    def _resolve_security(self) -> tuple[Optional[int], str]:
        """Return (existing_id_or_None, typed_name). When the combo text matches
        the selected item, use its id; otherwise treat the text as a (possibly
        new) security name."""
        text = self._security.currentText().strip()
        idx = self._security.currentIndex()
        if idx >= 0 and self._security.itemText(idx) == text:
            data = self._security.itemData(idx)
            if data is not None:
                return int(data), text
        m = self._security.findText(text)
        if m >= 0 and self._security.itemData(m) is not None:
            return int(self._security.itemData(m)), text
        return None, text


def _to_decimal(text: str) -> Optional[Decimal]:
    s = (text or "").strip().replace(",", "").lstrip("$£€")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _trim(value) -> str:
    """Format a REAL qty/price without trailing zeros."""
    s = f"{float(value):,.6f}".replace(",", "")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s
