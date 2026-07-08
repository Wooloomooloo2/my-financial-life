"""Investment transaction dialog — create + edit (ADR-048, ADR-093).

The single edit surface for investment-account transactions — opened by
**New Transaction** on an investment account (create) and by **double-clicking**
an investment row (edit). Deliberately investment-shaped, not the cash form:
the fields are **Date · Action · Instrument · Symbol · Security · (per-class
metadata) · Qty · Price · Commission · Accrued · Total cost · Status · Memo**
(no Payee / standing cash-amount box). The visible fields adapt to the action
**and the instrument class** so you only ever see what's relevant.

Field flow:
  * **Symbol drives Security.** Type a ticker and the Security name fills in —
    from an existing security with that symbol, or (when online with a Tiingo
    key) from a Tiingo metadata lookup. Picking an existing Security fills the
    Symbol the other way, plus its instrument class + metadata. A typed name
    with no match creates a new security of the chosen class.
  * **Instrument** (Stock / Bond / Option, ADR-093) governs the value maths via
    a per-security **price multiplier** (cash value of one unit at price = 1):
    a **bond** quotes as a % of par and trades in par multiples — multiplier =
    face / 100; an **option** trades in contracts of a multiplier (100) priced
    as a premium per share — multiplier = contract size; a **stock** is 1.
  * **Buy / Sell** — Quantity, Price and **Total cost** form a tri-field group:
    enter any **two** and the dialog fills the third. Total = qty × price ×
    multiplier ± commission. **Accrued interest** (bonds) is a separate cash
    term — paid to the seller on a buy, received on a sell — that is part of the
    cash but NOT the bond's cost basis (it nets against the first coupon). The
    signed cash impact is `∓ (total + accrued)`.
    **Reinvested dividend / Shares in-out** — Qty (+ optional Price for basis);
    cash impact 0. **Dividend / Interest / Cap-gain** — an Amount-in field
    replaces Qty/Price. **Cash in/out** — an Amount field only, no security.

The stored ``txn.amount`` is always the SIGNED CASH IMPACT, so cash balance =
SUM(amount) holds (ADR-043); ``txn.accrued_interest`` (ADR-093) carries the bond
accrued, which the holdings engine subtracts back out of basis/proceeds. When
editing a trade whose Qty/Price are unchanged, the original stored amount is
preserved (so a re-save never drifts a penny off the imported figure). The
dialog writes through the Repository and calls ``accept()``; the caller reloads.
Transfer actions (XIn/XOut) and option exercise/assignment are out of scope.
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
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop import txn_status
from mfl_desktop.db.repository import AccountSummary, Repository, SecurityRow, TransactionRow
from mfl_desktop.import_engine.qif_actions import is_reinvest
from mfl_desktop.prices import lookup_symbol_name
from mfl_desktop.ui import tokens
from mfl_desktop.ui.category_picker import make_category_picker, selected_category_id
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

# Instrument classes (label, stored instrument_type) — ADR-093.
_INSTRUMENTS: list[tuple[str, str]] = [
    ("Stock / ETF / fund", "stock"),
    ("Bond", "bond"),
    ("Option", "option"),
]

# The system income category an income/reinvest action routes to — mirrors
# qif_parser._INCOME_CATEGORY ("Income:Investment income").
_INCOME_PATH = ["Income", "Investment income"]

_DEFAULT_CONTRACT_SIZE = 100.0   # shares per option contract (ADR-093)


def _kind(action: str) -> str:
    """Classify an action into the UI behaviour group."""
    a = (action or "").strip().lower()
    if a == "buy":
        return "buy"
    if a == "sell":
        return "sell"
    if is_reinvest(a):          # reinvdiv / reinvlg / reinvsh / reinvint / reinvmd
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

        # Instrument class (ADR-093) — drives the per-class metadata rows, the
        # price label, and the value multiplier.
        self._instrument = QComboBox()
        for label, value in _INSTRUMENTS:
            self._instrument.addItem(label, value)
        self._instrument.currentIndexChanged.connect(self._on_instrument_changed)
        self._form.addRow("Instrument:", self._instrument)

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
        self._security_by_id: dict[int, SecurityRow] = {}   # ADR-093 metadata cache
        for s in self._repo.list_securities():
            self._security.addItem(s.name, s.id)
            self._symbol_by_sid[s.id] = s.symbol or ""
            self._security_by_id[s.id] = s
        self._security.setEditText("")
        self._security.currentIndexChanged.connect(self._on_security_changed)
        self._form.addRow("Security:", self._security)

        # ── Option metadata (ADR-093) ──
        self._underlying = QLineEdit()
        self._underlying.setPlaceholderText("underlying ticker, e.g. AAPL")
        self._form.addRow("Underlying:", self._underlying)

        self._strike = QLineEdit()
        self._strike.setPlaceholderText("strike price")
        self._form.addRow("Strike:", self._strike)

        self._expiry = make_date_edit()
        self._form.addRow("Expiry:", self._expiry)

        self._opt_kind = QComboBox()
        self._opt_kind.addItem("Call", "call")
        self._opt_kind.addItem("Put", "put")
        self._form.addRow("Type:", self._opt_kind)

        self._contract = QLineEdit()
        self._contract.setPlaceholderText("shares per contract (usually 100)")
        self._contract.textChanged.connect(self._on_multiplier_field_changed)
        self._form.addRow("Contract size:", self._contract)

        # ── Bond metadata (ADR-093) ──
        self._face = QLineEdit()
        self._face.setPlaceholderText("par per bond, e.g. 1000")
        self._face.textChanged.connect(self._on_multiplier_field_changed)
        self._form.addRow("Face value:", self._face)

        self._coupon = QLineEdit()
        self._coupon.setPlaceholderText("annual coupon %, e.g. 4")
        self._form.addRow("Coupon %:", self._coupon)

        self._maturity = make_date_edit()
        self._form.addRow("Maturity:", self._maturity)

        self._cusip = QLineEdit()
        self._cusip.setPlaceholderText("CUSIP / ISIN (optional)")
        self._form.addRow("CUSIP / ISIN:", self._cusip)

        self._qty = QLineEdit()
        self._qty.setPlaceholderText("shares")
        self._qty.textChanged.connect(lambda *_: self._on_trade_field_changed("qty"))
        self._form.addRow("Quantity:", self._qty)

        self._price = QLineEdit()
        self._price.setPlaceholderText("per share")
        self._price.textChanged.connect(lambda *_: self._on_trade_field_changed("price"))
        self._form.addRow("Price:", self._price)

        # Commission / fee (Buy/Sell only) — capitalised into the total cash, so
        # Total = quantity × price × multiplier ± commission. Blank = no fee.
        # Editing it re-solves whichever of qty/price/total the user left.
        self._commission = QLineEdit()
        self._commission.setPlaceholderText("fee (optional)")
        self._commission.textChanged.connect(self._on_commission_changed)
        self._form.addRow("Commission:", self._commission)

        # Accrued interest (bond Buy/Sell only, ADR-093) — paid to the seller on
        # a buy / received on a sell; part of the cash, NOT of cost basis.
        self._accrued = QLineEdit()
        self._accrued.setPlaceholderText("accrued interest (optional)")
        self._accrued.textChanged.connect(self._recompute_hint)
        self._form.addRow("Accrued interest:", self._accrued)

        # Total cost (Buy/Sell only) — the third leg of the
        # qty × price × multiplier = total relationship (net of commission, and
        # excluding accrued). Enter any two of qty/price/total and the dialog
        # fills the rest.
        self._total = QLineEdit()
        self._total.setPlaceholderText("principal (qty × price × size ± fee)")
        self._total.textChanged.connect(lambda *_: self._on_trade_field_changed("total"))
        self._form.addRow("Total cost:", self._total)

        self._amount = QLineEdit()
        self._amount.setPlaceholderText("cash amount")
        self._form.addRow("Amount:", self._amount)

        # ADR-086: a ledger category for the cash income/expense actions only
        # (dividends, interest, cap-gains, fees, manual cash in/out). Hidden for
        # portfolio moves (buy/sell/shares/split) — categorising those would
        # distort the cashflow reports. Resolve the default income category once.
        self._income_cat_id = self._repo.find_or_create_category_path(
            _INCOME_PATH, source="user",
        )
        # ADR-089: reinvested distributions default to the owner's configured
        # reinvest-dividend category (e.g. *Dividend Income*) when set, else the
        # seeded *Investment income*. Saving a reinvest under a category writes
        # this back (see _save), so it self-seeds and stays in sync with import.
        self._reinvest_cat_id = (
            self._repo.get_reinvest_dividend_category_id() or self._income_cat_id
        )
        # ADR-142: a cash dividend remembers its own category (e.g. *Dividend
        # Income*) rather than always seeding the generic *Investment income*.
        self._dividend_cat_id = (
            self._repo.get_dividend_category_id() or self._income_cat_id
        )
        self._category = make_category_picker(self._repo.list_categories_flat())
        self._category_touched = False
        self._category.currentIndexChanged.connect(self._on_category_touched)
        cat_line = self._category.lineEdit()
        if cat_line is not None:
            cat_line.textEdited.connect(self._on_category_touched)
        self._form.addRow("Category:", self._category)

        self._ratio = QLineEdit()
        self._ratio.setPlaceholderText("new shares per old — 5 for 5-for-1, 0.1 for 1-for-10")
        self._ratio.textChanged.connect(self._recompute_hint)
        self._form.addRow("Split ratio:", self._ratio)

        self._status = QComboBox()
        self._status.addItems(txn_status.labels())
        # ADR-142: a manual entry must never default to *matched* (that's the
        # OFX-download state, ADR-130) — default to *pending*, like the register
        # dialog.
        self._status.setCurrentText(txn_status.label(txn_status.PENDING))
        self._form.addRow("Status:", self._status)

        self._memo = QLineEdit()
        self._form.addRow("Memo:", self._memo)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        tokens.themed(self._hint, "QLabel { color: {muted}; font-size: 11px; }")
        outer.addWidget(self._hint)

        # ADR-107: hand-laid button row (see transaction_dialog.py) — Cancel /
        # Save / Save & New on the right, primary action where the eye lands.
        self._save_and_new_requested = False
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setAutoDefault(False)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._on_save)
        save_btn.setAutoDefault(False)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(cancel_btn)
        button_row.addWidget(save_btn)
        # ADR-107: "Save & New" only when creating — re-opening on an edit
        # makes no sense. It's the default (Enter) for fast multi-entry.
        if seed is None:
            save_new_btn = QPushButton("Save && New")
            save_new_btn.clicked.connect(self._on_save_and_new)
            save_new_btn.setDefault(True)
            save_new_btn.setAutoDefault(True)
            button_row.addWidget(save_new_btn)
        else:
            save_btn.setDefault(True)
            save_btn.setAutoDefault(True)
        outer.addLayout(button_row)

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

    def _set_label(self, field: QWidget, text: str) -> None:
        label = self._form.labelForField(field)
        if label is not None:
            label.setText(text)

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
            self._load_instrument_from_security(seed.security_id)
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
        # For a Buy/Sell, the stored amount IS the total cash (incl. any imported
        # commission AND accrued); back out accrued to seed the principal Total
        # so a re-save never drifts. Total = ∓amount − accrued (in magnitude).
        if _kind(seed.action or "") in ("buy", "sell"):
            accrued = seed.accrued_interest or Decimal("0")
            if seed.accrued_interest is not None:
                self._accrued.setText(f"{seed.accrued_interest:.2f}")
            principal = abs(seed.amount) - abs(accrued)
            self._total.setText(f"{principal:.2f}")
            if seed.commission is not None:
                self._commission.setText(f"{seed.commission:.2f}")
        self._amount.setText(f"{seed.amount:.2f}")
        self._memo.setText(seed.memo or "")
        self._status.setCurrentText(txn_status.label(seed.status or txn_status.PENDING))
        # ADR-086: preserve the row's stored category (shown only for the
        # categorisable actions). Signals are blocked, so this doesn't flip the
        # touched flag — the per-action default won't clobber it on edit.
        self._set_category(seed.category_id)

    def _load_instrument_from_security(self, security_id: int) -> None:
        """Set the Instrument combo + per-class metadata from a stored security
        (ADR-093). Signals are not blocked here — callers run while ``_loading``
        is True, so handlers self-suppress."""
        s = self._security_by_id.get(security_id)
        if s is None:
            return
        idx = self._instrument.findData(s.instrument_type or "stock")
        if idx >= 0:
            self._instrument.setCurrentIndex(idx)
        if s.face_value is not None:
            self._face.setText(_trim(s.face_value))
        if s.coupon_rate is not None:
            self._coupon.setText(_trim(s.coupon_rate))
        if s.maturity_date:
            self._maturity.setDate(QDate.fromString(s.maturity_date, "yyyy-MM-dd"))
        self._cusip.setText(s.cusip or "")
        self._underlying.setText(s.underlying_symbol or "")
        if s.strike is not None:
            self._strike.setText(_trim(s.strike))
        if s.expiry_date:
            self._expiry.setDate(QDate.fromString(s.expiry_date, "yyyy-MM-dd"))
        if s.option_type:
            oi = self._opt_kind.findData(s.option_type)
            if oi >= 0:
                self._opt_kind.setCurrentIndex(oi)
        self._contract.setText(
            _trim(s.contract_size) if s.contract_size is not None else ""
        )

    # ── action / instrument-driven field rules ──

    def _current_instrument(self) -> str:
        return self._instrument.currentData() or "stock"

    def _on_instrument_changed(self, _idx: int) -> None:
        self._apply_action_rules()
        # Multiplier may have changed (stock↔bond↔option) — re-solve the total.
        if not self._loading and _kind(self._current_action()) in ("buy", "sell"):
            self._solve_trade_field(self._trade_edit_order[-1])

    def _on_multiplier_field_changed(self, *_args) -> None:
        """Face value / contract size edited — the multiplier moved, so re-solve
        the tri-field group and refresh the hint."""
        if (
            not self._loading and not self._recomputing
            and _kind(self._current_action()) in ("buy", "sell")
        ):
            self._solve_trade_field(self._trade_edit_order[-1])
        self._recompute_hint()

    def _on_action_changed(self, _idx: int) -> None:
        self._apply_action_rules()

    def _apply_action_rules(self) -> None:
        kind = _kind(self._current_action())
        instrument = self._current_instrument()
        is_bond = instrument == "bond"
        is_option = instrument == "option"
        show_sec = kind != "cash"
        show_qty = kind in ("buy", "sell", "reinvest", "shares")
        show_price = kind in ("buy", "sell", "reinvest", "shares")
        show_total = kind in ("buy", "sell")
        show_commission = kind in ("buy", "sell")
        show_accrued = is_bond and kind in ("buy", "sell")
        show_amount = kind in ("income", "cash")
        show_ratio = kind == "split"
        # ADR-086 + ADR-089: cash income/expense **and** reinvests are
        # categorisable (a reinvest is zero-cash, so its category only feeds the
        # income report's reinvested-dividend valuation, never the cash totals).
        show_category = kind in ("income", "cash", "reinvest")

        # Instrument shown whenever an instrument is involved (not pure cash).
        self._set_row_visible(self._instrument, show_sec)
        self._set_row_visible(self._symbol, show_sec)
        self._set_row_visible(self._security, show_sec)
        # Option metadata.
        for w in (self._underlying, self._strike, self._expiry,
                  self._opt_kind, self._contract):
            self._set_row_visible(w, show_sec and is_option)
        # Bond metadata.
        for w in (self._face, self._coupon, self._maturity, self._cusip):
            self._set_row_visible(w, show_sec and is_bond)
        self._set_row_visible(self._qty, show_qty)
        self._set_row_visible(self._price, show_price)
        self._set_row_visible(self._commission, show_commission)
        self._set_row_visible(self._accrued, show_accrued)
        self._set_row_visible(self._total, show_total)
        self._set_row_visible(self._amount, show_amount)
        self._set_row_visible(self._ratio, show_ratio)
        self._set_row_visible(self._category, show_category)

        # Per-instrument labels on the shared Quantity / Price rows.
        if is_bond:
            self._set_label(self._qty, "Quantity (bonds):")
            self._set_label(self._price, "Price (% of par):")
        elif is_option:
            self._set_label(self._qty, "Contracts:")
            self._set_label(self._price, "Premium / share:")
        else:
            self._set_label(self._qty, "Quantity:")
            self._set_label(self._price, "Price:")

        # On create, default income actions to *Investment income* and the
        # manual Cash action to Uncategorised — until the user picks otherwise.
        # ADR-142: a cash Dividend defaults to its own remembered category
        # (self._dividend_cat_id, e.g. *Dividend Income*), which self-seeds when
        # the user files one (see _save). Edit mode keeps the row's stored
        # category (seeded in _populate_from_seed).
        if (
            show_category and self._seed is None
            and not self._loading and not self._category_touched
        ):
            self._set_category(
                self._reinvest_cat_id if kind == "reinvest"
                else self._dividend_cat_id if self._current_action() == "Div"
                else self._income_cat_id if kind == "income"
                else self._repo.uncategorised_id()
            )
        # Entering Buy/Sell with a qty + price already typed → fill the total
        # (unless the user/seed already supplied one).
        if show_total and not self._total.text().strip() and not self._loading:
            self._solve_trade_field("total")
        self._recompute_hint()

    def _current_action(self) -> str:
        return self._action.currentData() or ""

    def _multiplier(self) -> Decimal:
        """Value multiplier for the chosen instrument (ADR-093): stock → 1;
        bond → face / 100; option → contract size (default 100). Falls back to 1
        when the driving field is blank/invalid so the maths stays well-defined."""
        instrument = self._current_instrument()
        if instrument == "bond":
            face = _to_decimal(self._face.text())
            if face is not None and face > 0:
                return face / Decimal(100)
            return Decimal(1)
        if instrument == "option":
            size = _to_decimal(self._contract.text())
            if size is not None and size > 0:
                return size
            return Decimal(str(_DEFAULT_CONTRACT_SIZE))
        return Decimal(1)

    # ── category (ADR-086) ──

    def _on_category_touched(self, *_args) -> None:
        """Mark the category as user-chosen so the per-action default stops
        overriding it (programmatic changes block signals, so don't reach here)."""
        if not self._loading:
            self._category_touched = True

    def _set_category(self, category_id: Optional[int]) -> None:
        """Select the picker to a category id without tripping the touched flag."""
        line = self._category.lineEdit()
        self._category.blockSignals(True)
        if line is not None:
            line.blockSignals(True)
        try:
            idx = self._category.findData(category_id) if category_id is not None else -1
            if idx >= 0:
                self._category.setCurrentIndex(idx)
            elif line is not None:
                self._category.setCurrentIndex(-1)
                line.setText("")
        finally:
            self._category.blockSignals(False)
            if line is not None:
                line.blockSignals(False)

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
        """Commission is the fourth term (Total = qty × price × mult ±
        commission). A change re-solves whichever leg the user last left."""
        if (
            not self._loading
            and not self._recomputing
            and _kind(self._current_action()) in ("buy", "sell")
        ):
            self._solve_trade_field(self._trade_edit_order[-1])
        self._recompute_hint()

    def _solve_trade_field(self, target: str) -> None:
        """Fill `target` from the other two of {qty, price, total} plus the
        commission and the instrument multiplier, if both legs are present (and
        the divisor is non-zero). Total = qty × price × m + s·commission, where
        m is the value multiplier and s = +1 for a Buy (the fee adds to the cash
        out) and −1 for a Sell (the fee nets off the proceeds). No-op when a
        needed value is missing."""
        qty = _to_decimal(self._qty.text())
        price = _to_decimal(self._price.text())
        total = _to_decimal(self._total.text())
        comm = _to_decimal(self._commission.text()) or Decimal(0)
        m = self._multiplier()
        s = Decimal(1) if _kind(self._current_action()) == "buy" else Decimal(-1)
        self._recomputing = True
        try:
            if target == "total" and qty is not None and price is not None:
                self._total.setText(_money(qty * price * m + s * comm))
            elif (
                target == "price" and qty not in (None, Decimal(0))
                and total is not None and m != 0
            ):
                self._price.setText(_trim((total - s * comm) / (qty * m)))
            elif (
                target == "qty" and price not in (None, Decimal(0))
                and total is not None and m != 0
            ):
                self._qty.setText(_trim((total - s * comm) / (price * m)))
        finally:
            self._recomputing = False

    def _recompute_hint(self, *_args) -> None:
        kind = _kind(self._current_action())
        instrument = self._current_instrument()
        if kind == "buy":
            base = ("Buy — enter any two of quantity, price, total; the third "
                    "fills in. Total = quantity × price × size + commission.")
        elif kind == "sell":
            base = ("Sell — enter any two of quantity, price, total; the third "
                    "fills in. Total = quantity × price × size − commission.")
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
            if instrument == "bond":
                base += (" Price is a % of par; size = face ÷ 100. Accrued "
                         "interest is cash, not cost basis.")
            elif instrument == "option":
                base += " Premium per share; size = contract size (×100)."
            total = _to_decimal(self._total.text())
            accrued = _to_decimal(self._accrued.text()) or Decimal(0)
            if total is not None:
                gross = abs(total) + abs(accrued)
                signed = -gross if kind == "buy" else gross
                base += f"  →  cash {signed:,.2f}"
            if instrument == "option" and kind == "sell":
                base += "  ·  Expire worthless = a Sell at price 0."
        if kind != "cash":
            base += "  ·  Type a ticker to auto-fill the security name (online)."
        self._hint.setText(base)

    # ── symbol ⇄ security ──

    def _on_security_changed(self, idx: int) -> None:
        """Selecting an existing security mirrors its stored ticker into Symbol
        and loads its instrument class + metadata (ADR-093)."""
        sid = self._security.itemData(idx)
        if sid is not None:
            sid = int(sid)
            sym = self._symbol_by_sid.get(sid, "")
            self._symbol.setText(sym)
            self._last_lookup_symbol = sym.strip().upper()
            was_loading = self._loading
            self._loading = True
            try:
                self._load_instrument_from_security(sid)
            finally:
                self._loading = was_loading
            self._apply_action_rules()

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

    def _instrument_metadata(self) -> dict:
        """The instrument-class metadata to persist on the security (ADR-093).
        Every column is explicit (value or None) so a class switch clears the
        columns that no longer apply, and ``price_multiplier`` always matches."""
        instrument = self._current_instrument()
        meta = {
            "instrument_type": instrument,
            "price_multiplier": float(self._multiplier()),
            "face_value": None, "coupon_rate": None, "maturity_date": None,
            "cusip": None, "underlying_symbol": None, "strike": None,
            "expiry_date": None, "option_type": None, "contract_size": None,
        }
        if instrument == "bond":
            face = _to_decimal(self._face.text())
            coupon = _to_decimal(self._coupon.text())
            meta["face_value"] = float(face) if face is not None else None
            meta["coupon_rate"] = float(coupon) if coupon is not None else None
            meta["maturity_date"] = self._maturity.date().toString("yyyy-MM-dd")
            meta["cusip"] = self._cusip.text().strip() or None
        elif instrument == "option":
            strike = _to_decimal(self._strike.text())
            size = _to_decimal(self._contract.text())
            meta["underlying_symbol"] = self._underlying.text().strip() or None
            meta["strike"] = float(strike) if strike is not None else None
            meta["expiry_date"] = self._expiry.date().toString("yyyy-MM-dd")
            meta["option_type"] = self._opt_kind.currentData()
            meta["contract_size"] = (
                float(size) if size is not None and size > 0
                else _DEFAULT_CONTRACT_SIZE
            )
        return meta

    def _on_save(self) -> None:
        action = self._current_action()
        kind = _kind(action)
        instrument = self._current_instrument()

        # Bond sanity: a face value is what makes the %-of-par maths meaningful.
        if instrument == "bond" and kind in ("buy", "sell"):
            face = _to_decimal(self._face.text())
            if face is None or face <= 0:
                QMessageBox.warning(
                    self, "Save transaction",
                    "Enter the bond's face value (par per bond, e.g. 1000).",
                )
                return

        # Security (+ its ticker + instrument metadata, on the security master).
        security_id: Optional[int] = None
        if kind != "cash":
            sid, name = self._resolve_security()
            if sid is None and not name:
                QMessageBox.warning(
                    self, "Save transaction",
                    "Pick or type a security for this action.",
                )
                return
            meta = self._instrument_metadata()
            typed_symbol = self._symbol.text().strip()
            if sid is not None:
                security_id = sid
                try:
                    self._repo.update_security(
                        sid,
                        symbol=(typed_symbol
                                if typed_symbol != (self._symbol_by_sid.get(sid, "") or "")
                                else None),
                        **meta,
                    )
                except ValueError as e:
                    QMessageBox.warning(self, "Save transaction", str(e))
                    return
            else:
                security_id = self._repo.get_or_create_security(
                    name, typed_symbol, **meta,
                )

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
        if kind in ("buy", "reinvest"):
            # A Sell-to-close at price 0 is a legitimate option expiry, so only
            # require a positive price on buys and reinvests.
            if price is None or price <= 0:
                QMessageBox.warning(self, "Save transaction", "Enter a positive price.")
                return
        if kind == "sell":
            if price is None or price < 0:
                QMessageBox.warning(
                    self, "Save transaction",
                    "Enter a price (0 is allowed for an expired option).",
                )
                return

        accrued = (
            _to_decimal(self._accrued.text())
            if (instrument == "bond" and kind in ("buy", "sell")) else None
        )
        amount = self._compute_amount(kind, qty, price, accrued)
        if amount is None:
            QMessageBox.warning(self, "Save transaction", "Enter a cash amount.")
            return
        if kind == "income" and amount < 0:
            QMessageBox.warning(
                self, "Save transaction", "Income should be a positive amount.",
            )
            return

        # ADR-086 + ADR-089: the categorisable actions — cash income/expense
        # **and** reinvests — take the chosen category (defaulting to
        # *Investment income* for the income-like ones); all other actions stay
        # Uncategorised.
        if kind in ("income", "cash", "reinvest"):
            category_id = selected_category_id(self._category)
            if category_id is None:
                category_id = (
                    self._reinvest_cat_id if kind == "reinvest"
                    else self._dividend_cat_id if action == "Div"
                    else self._income_cat_id if kind == "income"
                    else self._repo.uncategorised_id()
                )
        else:
            category_id = self._repo.uncategorised_id()

        # ADR-089: filing a reinvest under a category makes it the default for
        # future reinvests (import + dialog). ADR-142: same for a cash dividend.
        if kind == "reinvest" and category_id != self._repo.uncategorised_id():
            self._repo.set_reinvest_dividend_category_id(category_id)
        if action == "Div" and category_id != self._repo.uncategorised_id():
            self._repo.set_dividend_category_id(category_id)
        posted_date = self._date.date().toString("yyyy-MM-dd")
        status = txn_status.key_for_label(self._status.currentText())
        memo = self._memo.text().strip()
        # Commission is a Buy/Sell-only field; already folded into `amount`, so
        # it's stored purely as metadata (basis uses abs(amount) − accrued).
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
                    accrued_interest=accrued,
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
                    accrued_interest=accrued,
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

    def _on_save_and_new(self) -> None:
        """ADR-107: commit like Save, but flag the register to reopen a fresh
        investment dialog afterwards for fast multi-entry. Reuses _on_save so
        the two paths never diverge; clears the flag if validation bailed."""
        self._save_and_new_requested = True
        self._on_save()
        if self.result() != QDialog.Accepted:
            self._save_and_new_requested = False

    def save_and_new_requested(self) -> bool:
        """True when the user clicked Save & New, so the register should reopen
        a fresh dialog on the same account after this one commits (ADR-107)."""
        return self._save_and_new_requested

    def _compute_amount(
        self, kind: str, qty: Optional[Decimal], price: Optional[Decimal],
        accrued: Optional[Decimal],
    ) -> Optional[Decimal]:
        """The signed cash impact for this row. For Buy/Sell it is
        ``∓ (total + accrued)`` — the principal (qty × price × multiplier ±
        commission, the authoritative Total leg) plus any bond accrued interest
        (paid on a buy, received on a sell). Both already net commission in; the
        holdings engine subtracts accrued back out of basis/proceeds (ADR-093).
        Share transfers / reinvests are zero; income / cash are user-entered.
        Falls back to qty × price × multiplier if Total is left blank."""
        if kind in ("income", "cash"):
            return _to_decimal(self._amount.text())
        if kind in ("reinvest", "shares", "split"):
            return Decimal("0.00")
        # buy / sell — the Total field is the principal driving the cash impact.
        total = _to_decimal(self._total.text())
        if total is None and qty is not None and price is not None:
            comm = _to_decimal(self._commission.text()) or Decimal(0)
            m = self._multiplier()
            s = Decimal(1) if kind == "buy" else Decimal(-1)
            total = qty * price * m + s * comm
        if total is None:
            return None
        gross = abs(total) + abs(accrued or Decimal(0))
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
