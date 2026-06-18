"""Investment transaction dialog — create + edit (ADR-048).

The single edit surface for investment-account transactions — opened by
**New Transaction** on an investment account (create) and by **double-clicking**
an investment row (edit). Deliberately investment-shaped, not the cash form:
the fields are **Date · Action · Symbol · Security · Qty · Price · Commission ·
Total cost · Status · Memo** (no Payee / standing cash-amount box). The visible
fields adapt to the action so you only ever see what's relevant.

Field flow:
  * **Symbol drives Security.** Type a ticker and the Security name fills in —
    from an existing security with that symbol, or (when online with a Tiingo
    key) from a Tiingo metadata lookup. Picking an existing Security fills the
    Symbol the other way. A typed name with no match creates a new security.
  * **Buy / Sell** — Quantity, Price and **Total cost** form a tri-field group:
    enter any **two** and the dialog fills the third. An optional **Commission**
    is the fourth term — Total = qty × price + commission (Buy) / − commission
    (Sell) — and editing it re-solves the leg you left blank. The signed cash
    impact is the total (`∓ total`), which already nets the fee in, so an
    imported amount that carries commission survives a re-save unchanged.
    **Reinvested dividend / Shares in-out** — Qty (+ optional Price for basis);
    cash impact 0. **Dividend / Interest / Cap-gain** — an Amount-in field
    replaces Qty/Price. **Cash in/out** — an Amount field only, no security.

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
from mfl_desktop.ui import tokens
from mfl_desktop.ui.date_widgets import make_date_edit

# Curated action list for manual entry (label, canonical QIF action stored in
# txn.action — must match the strings the holdings/returns engines classify via
# qif_actions). Whole-account transfer (XIn/XOut) deliberately omitted in v1;
# StkSplit added in ADR-054 (ratio in the quantity field).
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
    ("Stock split", "StkSplit"),
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
    if a in ("stksplit", "stocksplit"):
        return "split"
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
        # Tri-field solver state for Buy/Sell (qty ⇄ price ⇄ total cost): the
        # user enters any two and the third fills in. `_recomputing` guards the
        # programmatic setText from re-triggering the handler; `_trade_edit_order`
        # tracks most-recently-edited first, so the field touched longest ago is
        # the one we recompute (total is the default target).
        self._recomputing = False
        self._trade_edit_order: list[str] = ["price", "qty", "total"]
        self.setWindowTitle(
            "Edit investment transaction" if seed else "New investment transaction"
        )
        self.setMinimumWidth(460)

        outer = QVBoxLayout(self)
        self._form = QFormLayout()
        outer.addLayout(self._form)

        acct_label = QLabel(f"{account.name}  ·  {account.currency}")
        tokens.themed(acct_label, "QLabel { color: {muted_strong}; }")
        self._form.addRow("Account:", acct_label)

        self._date = make_date_edit()
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
        self._qty.textChanged.connect(lambda *_: self._on_trade_field_changed("qty"))
        self._form.addRow("Quantity:", self._qty)

        self._price = QLineEdit()
        self._price.setPlaceholderText("per share")
        self._price.textChanged.connect(lambda *_: self._on_trade_field_changed("price"))
        self._form.addRow("Price:", self._price)

        # Commission / fee (Buy/Sell only) — capitalised into the total cash, so
        # Total = quantity × price ± commission. Blank = no fee. Editing it
        # re-solves whichever of qty/price/total the user left for the dialog.
        self._commission = QLineEdit()
        self._commission.setPlaceholderText("fee (optional)")
        self._commission.textChanged.connect(self._on_commission_changed)
        self._form.addRow("Commission:", self._commission)

        # Total cost (Buy/Sell only) — the third leg of the qty × price = total
        # relationship (net of commission). Enter any two of qty/price/total and
        # the dialog fills the rest.
        self._total = QLineEdit()
        self._total.setPlaceholderText("total cash (incl. commission)")
        self._total.textChanged.connect(lambda *_: self._on_trade_field_changed("total"))
        self._form.addRow("Total cost:", self._total)

        self._amount = QLineEdit()
        self._amount.setPlaceholderText("cash amount")
        self._form.addRow("Amount:", self._amount)

        self._ratio = QLineEdit()
        self._ratio.setPlaceholderText("new shares per old — 5 for 5-for-1, 0.1 for 1-for-10")
        self._ratio.textChanged.connect(self._recompute_hint)
        self._form.addRow("Split ratio:", self._ratio)

        self._status = QComboBox()
        self._status.addItems(_STATUSES)
        self._status.setCurrentText("Cleared")
        self._form.addRow("Status:", self._status)

        self._memo = QLineEdit()
        self._form.addRow("Memo:", self._memo)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        tokens.themed(self._hint, "QLabel { color: {muted}; font-size: 11px; }")
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
            # A StkSplit stores its ratio in the quantity field — seed the ratio
            # input, not the share-quantity input.
            if _kind(seed.action or "") == "split":
                self._ratio.setText(_trim(seed.quantity))
            else:
                self._qty.setText(_trim(seed.quantity))
        if seed.price is not None:
            self._price.setText(_trim(seed.price))
        # For a Buy/Sell, the stored amount IS the total cash cost (incl. any
        # imported commission); seed it + the commission straight in so a re-save
        # never drifts. The Total = qty × price ± commission relationship holds
        # for imported rows because that's how the amount was computed.
        if _kind(seed.action or "") in ("buy", "sell"):
            self._total.setText(f"{abs(seed.amount):.2f}")
            if seed.commission is not None:
                self._commission.setText(f"{seed.commission:.2f}")
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
        show_total = kind in ("buy", "sell")
        show_commission = kind in ("buy", "sell")
        show_amount = kind in ("income", "cash")
        show_ratio = kind == "split"

        self._set_row_visible(self._symbol, show_sec)
        self._set_row_visible(self._security, show_sec)
        self._set_row_visible(self._qty, show_qty)
        self._set_row_visible(self._price, show_price)
        self._set_row_visible(self._commission, show_commission)
        self._set_row_visible(self._total, show_total)
        self._set_row_visible(self._amount, show_amount)
        self._set_row_visible(self._ratio, show_ratio)
        # Entering Buy/Sell with a qty + price already typed → fill the total
        # (unless the user/seed already supplied one).
        if show_total and not self._total.text().strip() and not self._loading:
            self._solve_trade_field("total")
        self._recompute_hint()

    def _current_action(self) -> str:
        return self._action.currentData() or ""

    # ── tri-field solver (qty ⇄ price ⇄ total cost) ──

    def _on_trade_field_changed(self, field: str) -> None:
        """A user edit to quantity / price / total. For Buy/Sell, recompute the
        third leg from the two most-recently-edited fields; always refresh the
        hint (quantity + price are also used by reinvest / shares actions)."""
        if (
            not self._loading
            and not self._recomputing
            and _kind(self._current_action()) in ("buy", "sell")
        ):
            order = self._trade_edit_order
            if field in order:
                order.remove(field)
            order.insert(0, field)
            self._solve_trade_field(order[-1])   # least-recently edited = target
        self._recompute_hint()

    def _on_commission_changed(self, *_args) -> None:
        """Commission is the fourth term (Total = qty × price ± commission). A
        change re-solves whichever leg the user last left for the dialog."""
        if (
            not self._loading
            and not self._recomputing
            and _kind(self._current_action()) in ("buy", "sell")
        ):
            self._solve_trade_field(self._trade_edit_order[-1])
        self._recompute_hint()

    def _solve_trade_field(self, target: str) -> None:
        """Fill `target` from the other two of {qty, price, total} plus the
        commission, if both legs are present (and the divisor is non-zero).
        Total = qty × price + s·commission, where s = +1 for a Buy (the fee adds
        to the cash out) and −1 for a Sell (the fee nets off the proceeds).
        No-op when a needed value is missing."""
        qty = _to_decimal(self._qty.text())
        price = _to_decimal(self._price.text())
        total = _to_decimal(self._total.text())
        comm = _to_decimal(self._commission.text()) or Decimal(0)
        s = Decimal(1) if _kind(self._current_action()) == "buy" else Decimal(-1)
        self._recomputing = True
        try:
            if target == "total" and qty is not None and price is not None:
                self._total.setText(_money(qty * price + s * comm))
            elif target == "price" and qty not in (None, Decimal(0)) and total is not None:
                self._price.setText(_trim((total - s * comm) / qty))
            elif target == "qty" and price not in (None, Decimal(0)) and total is not None:
                self._qty.setText(_trim((total - s * comm) / price))
        finally:
            self._recomputing = False

    def _recompute_hint(self, *_args) -> None:
        kind = _kind(self._current_action())
        if kind == "buy":
            base = ("Buy — enter any two of quantity, price, total cost; the third "
                    "fills in. Total = quantity × price + commission; cash out = −total.")
        elif kind == "sell":
            base = ("Sell — enter any two of quantity, price, total cost; the third "
                    "fills in. Total = quantity × price − commission; cash in = +total.")
        elif kind == "reinvest":
            base = "Reinvested dividend — no cash moves; counts as income."
        elif kind == "shares":
            base = "Share transfer — no cash moves. Price is optional (cost basis)."
        elif kind == "split":
            base = ("Stock split — no cash moves. Your shares ×ratio and cost "
                    "per share ÷ratio (total cost unchanged).")
            r = _to_decimal(self._ratio.text())
            if r is not None and r > 0:
                base += f"  →  {r:g}-for-1"
        elif kind == "income":
            base = "Enter the cash received (positive)."
        else:  # cash
            base = "Enter the signed cash amount (− for money out)."
        if kind in ("buy", "sell"):
            total = _to_decimal(self._total.text())
            if total is not None:
                gross = abs(total)
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

        # Quantity / price (only relevant to share-moving actions). A stock
        # split carries its RATIO in the quantity field (new shares per old).
        wants_qty = kind in ("buy", "sell", "reinvest", "shares")
        qty = _to_decimal(self._qty.text()) if wants_qty else None
        price = _to_decimal(self._price.text()) if wants_qty else None
        if kind == "split":
            qty = _to_decimal(self._ratio.text())
            if qty is None or qty <= 0:
                QMessageBox.warning(
                    self, "Save transaction",
                    "Enter a positive split ratio (e.g. 5 for a 5-for-1 split, "
                    "0.1 for a reverse 1-for-10).",
                )
                return
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
        # Commission is a Buy/Sell-only field; already folded into `amount`, so
        # it's stored purely as metadata (basis uses abs(amount)). None elsewhere.
        commission = (
            _to_decimal(self._commission.text()) if kind in ("buy", "sell") else None
        )

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
                    commission=commission,
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
                    commission=commission,
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
        """The signed cash impact for this row. For Buy/Sell it is the **total
        cost** field (the authoritative leg of qty × price = total) — seeded from
        the stored amount on edit, so a re-save never drifts off the imported
        figure (incl. commission); share transfers / reinvests are zero; income /
        cash are user-entered. Falls back to qty × price if total is left blank."""
        if kind in ("income", "cash"):
            return _to_decimal(self._amount.text())
        if kind in ("reinvest", "shares", "split"):
            return Decimal("0.00")
        # buy / sell — total cost field drives the signed cash impact. It already
        # nets commission in (Total = qty × price ± commission); fall back to that
        # formula only if the user left Total blank.
        total = _to_decimal(self._total.text())
        if total is None and qty is not None and price is not None:
            comm = _to_decimal(self._commission.text()) or Decimal(0)
            s = Decimal(1) if kind == "buy" else Decimal(-1)
            total = qty * price + s * comm
        if total is None:
            return None
        gross = abs(total)
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


def _money(value) -> str:
    """Format a money total to 2 decimal places (no thousands separators, so it
    re-parses cleanly through ``_to_decimal``)."""
    return f"{Decimal(value):.2f}"
