"""Loan account create/edit dialog (ADR-095).

Captures a loan's terms — original amount, principal already paid, rate +
compounding, term, payment (typed or calculated from the term), optional extra
payment, start date + payment day, and the tracking model (principal+interest
vs whole amount; for the split, the paying account + whether interest is booked
in the loan account or on the paying account). A live preview shows the monthly
payment, payoff date, and total interest from ``loan_calc``.

On create it calls ``Repository.create_loan_account`` and (optionally) adds an
ADR-058 pay-down goal so the payoff shows in the budget. Edit updates the
mutable terms via ``update_loan`` (the original amount / principal paid are
fixed once the account exists, since they set its opening balance).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import Loan, Repository
from mfl_desktop import loan_calc
from mfl_desktop.ui import tokens
from mfl_desktop.ui.category_picker import make_category_picker, selected_category_id
from mfl_desktop.ui.date_widgets import make_date_edit

_COMPOUNDING = [("Monthly", "monthly"), ("Daily", "daily"), ("Annually", "annually")]
_TRACK = [("Principal + interest", "split"), ("Whole amount", "whole")]
_INTEREST_SRC = [
    ("Interest booked in the loan account", "loan"),
    ("Interest paid from the paying account", "payment"),
]
_CURRENCIES = ["GBP", "USD", "EUR", "JPY"]


class LoanDialog(QDialog):
    """Create or edit a loan account + its terms."""

    def __init__(
        self, repo: Repository, account_id: Optional[int] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._account_id = account_id           # None = create
        self._loan: Optional[Loan] = repo.get_loan(account_id) if account_id else None
        self.created_account_id: Optional[int] = None
        self._loading = True
        is_edit = self._loan is not None
        self.setWindowTitle("Edit loan" if is_edit else "New loan")
        self.setMinimumWidth(480)

        outer = QVBoxLayout(self)
        form = QFormLayout()
        outer.addLayout(form)
        self._form = form

        # Paying accounts = non-liability cash-like accounts.
        self._accounts = [
            a for a in repo.list_accounts() if not a.is_liability
        ]

        self._name = QLineEdit()
        form.addRow("Name:", self._name)

        self._currency = QComboBox()
        self._currency.addItems(_CURRENCIES)
        form.addRow("Currency:", self._currency)

        self._original = QLineEdit()
        self._original.setPlaceholderText("original loan amount")
        self._original.textChanged.connect(self._refresh_preview)
        form.addRow("Original amount:", self._original)

        self._paid = QLineEdit()
        self._paid.setPlaceholderText("principal already paid (0 if new)")
        self._paid.textChanged.connect(self._refresh_preview)
        form.addRow("Principal already paid:", self._paid)

        self._rate = QLineEdit()
        self._rate.setPlaceholderText("annual interest rate %, e.g. 5.5")
        self._rate.textChanged.connect(self._refresh_preview)
        form.addRow("Interest rate %:", self._rate)

        self._compounding = QComboBox()
        for label, val in _COMPOUNDING:
            self._compounding.addItem(label, val)
        self._compounding.currentIndexChanged.connect(self._refresh_preview)
        form.addRow("Interest applied:", self._compounding)

        self._term = QLineEdit()
        self._term.setPlaceholderText("term in months, e.g. 360 (30 yr)")
        self._term.textChanged.connect(self._refresh_preview)
        form.addRow("Term (months):", self._term)

        self._payment = QLineEdit()
        self._payment.setPlaceholderText("leave blank to calculate from the term")
        self._payment.textChanged.connect(self._refresh_preview)
        form.addRow("Monthly payment:", self._payment)

        self._extra = QLineEdit()
        self._extra.setPlaceholderText("optional extra each month")
        self._extra.textChanged.connect(self._refresh_preview)
        form.addRow("Extra payment:", self._extra)

        self._start = make_date_edit()
        form.addRow("Start date:", self._start)

        self._pay_day = QLineEdit()
        self._pay_day.setPlaceholderText("day of month (1–31)")
        form.addRow("Payment day:", self._pay_day)

        self._track = QComboBox()
        for label, val in _TRACK:
            self._track.addItem(label, val)
        self._track.currentIndexChanged.connect(self._on_track_changed)
        form.addRow("Track:", self._track)

        self._pay_acct = QComboBox()
        self._pay_acct.addItem("— pick an account —", None)
        for a in self._accounts:
            self._pay_acct.addItem(f"{a.name} ({a.currency})", a.id)
        form.addRow("Paying account:", self._pay_acct)

        self._interest_src = QComboBox()
        for label, val in _INTEREST_SRC:
            self._interest_src.addItem(label, val)
        form.addRow("Interest:", self._interest_src)

        self._interest_cat = make_category_picker(repo.list_categories_flat())
        form.addRow("Interest category:", self._interest_cat)

        # Budget pay-down goal (create only, when a budget exists).
        self._goal_cb = QCheckBox("Track the pay-off in my budget")
        self._has_budget = bool(repo.list_budgets())
        self._goal_cb.setChecked(self._has_budget and not is_edit)
        self._goal_cb.setEnabled(self._has_budget and not is_edit)
        if not is_edit:
            form.addRow("", self._goal_cb)

        self._preview = QLabel("")
        self._preview.setWordWrap(True)
        tokens.themed(self._preview, "QLabel { color: {muted_strong}; font-size: 11px; }")
        outer.addWidget(self._preview)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setDefault(True)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        if is_edit:
            self._populate(self._loan)
        else:
            self._pay_day.setText("1")
        self._loading = False
        self._on_track_changed()
        self._refresh_preview()

    # ── population (edit) ──

    def _populate(self, loan: Loan) -> None:
        acct = next(
            (a for a in self._repo.list_accounts(include_closed=True)
             if a.id == loan.account_id), None,
        )
        if acct is not None:
            self._name.setText(acct.name)
            i = self._currency.findText(acct.currency)
            if i >= 0:
                self._currency.setCurrentIndex(i)
        self._currency.setEnabled(False)
        # Original amount + principal paid set the opening balance — fixed now.
        self._original.setText(f"{loan.original_amount:.2f}")
        self._original.setReadOnly(True)
        self._paid.setText(f"{loan.principal_paid:.2f}")
        self._paid.setReadOnly(True)
        self._rate.setText(f"{loan.interest_rate:g}")
        ci = self._compounding.findData(loan.compounding)
        if ci >= 0:
            self._compounding.setCurrentIndex(ci)
        self._term.setText(str(loan.term_months) if loan.term_months else "")
        if loan.payment is not None:
            self._payment.setText(f"{loan.payment:.2f}")
        if loan.extra_payment:
            self._extra.setText(f"{loan.extra_payment:.2f}")
        try:
            self._start.setDate(QDate.fromString(loan.start_date, "yyyy-MM-dd"))
        except Exception:
            pass
        self._pay_day.setText(str(loan.payment_day))
        ti = self._track.findData(loan.track_mode)
        if ti >= 0:
            self._track.setCurrentIndex(ti)
        if loan.payment_account_id is not None:
            pi = self._pay_acct.findData(loan.payment_account_id)
            if pi >= 0:
                self._pay_acct.setCurrentIndex(pi)
        si = self._interest_src.findData(loan.interest_source)
        if si >= 0:
            self._interest_src.setCurrentIndex(si)
        if loan.interest_category_id is not None:
            idx = self._interest_cat.findData(loan.interest_category_id)
            if idx >= 0:
                self._interest_cat.setCurrentIndex(idx)

    # ── rules / preview ──

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        field.setVisible(visible)
        label = self._form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def _on_track_changed(self, *_a) -> None:
        split = self._track.currentData() == "split"
        # Paying account is needed in both modes (the cash has to come from
        # somewhere); the interest source + category only matter when splitting.
        self._set_row_visible(self._interest_src, split)
        self._set_row_visible(self._interest_cat, split)
        self._refresh_preview()

    def _refresh_preview(self, *_a) -> None:
        if self._loading:
            return
        principal = self._dec(self._original) or Decimal("0")
        paid = self._dec(self._paid) or Decimal("0")
        current = principal - paid
        rate = self._float(self._rate)
        compounding = self._compounding.currentData()
        term = self._int(self._term)
        payment = self._dec(self._payment)
        extra = self._dec(self._extra) or Decimal("0")
        if current <= 0 or rate is None:
            self._preview.setText("Enter the amount and rate to preview the schedule.")
            return
        if payment is None:
            if not term:
                self._preview.setText(
                    "Enter a monthly payment, or a term to calculate one.")
                return
            payment = loan_calc.required_payment(principal, rate, compounding, term)
        try:
            sched = loan_calc.compute_schedule(
                current_principal=current, annual_rate_pct=rate,
                compounding=compounding, payment=payment,
                start_date=self._start.date().toString("yyyy-MM-dd"),
                payment_day=self._int(self._pay_day) or 1, extra_payment=extra,
            )
        except Exception as e:  # noqa: BLE001
            self._preview.setText(f"Can't preview: {e}")
            return
        if sched.negative_amortization:
            self._preview.setText(
                f"⚠ A payment of {payment:,.2f} doesn't cover the interest — "
                f"the balance would never reduce. Increase the payment."
            )
            return
        cur = self._currency.currentText()
        self._preview.setText(
            f"Monthly payment {cur} {payment:,.2f}  ·  "
            f"{sched.n_payments} payments  ·  paid off {sched.payoff_date}  ·  "
            f"total interest {cur} {sched.total_interest:,.2f}"
        )

    # ── save ──

    def _on_save(self) -> None:
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, "Save loan", "Give the loan a name.")
            return
        principal = self._dec(self._original)
        if self._account_id is None and (principal is None or principal <= 0):
            QMessageBox.warning(self, "Save loan", "Enter the original loan amount.")
            return
        rate = self._float(self._rate)
        if rate is None or rate < 0:
            QMessageBox.warning(self, "Save loan", "Enter the interest rate.")
            return
        term = self._int(self._term)
        payment = self._dec(self._payment)
        if payment is None and not term:
            QMessageBox.warning(
                self, "Save loan",
                "Enter a monthly payment, or a term so one can be calculated.",
            )
            return
        track = self._track.currentData()
        pay_acct = self._pay_acct.currentData()
        if pay_acct is None:
            QMessageBox.warning(
                self, "Save loan",
                "Pick the account the payments come from.",
            )
            return
        paid = self._dec(self._paid) or Decimal("0")
        extra = self._dec(self._extra) or Decimal("0")
        compounding = self._compounding.currentData()
        start = self._start.date().toString("yyyy-MM-dd")
        pay_day = self._int(self._pay_day) or 1
        interest_src = self._interest_src.currentData()
        interest_cat = (
            selected_category_id(self._interest_cat) if track == "split" else None
        )

        try:
            if self._account_id is None:
                acct_id = self._repo.create_loan_account(
                    name=name, currency=self._currency.currentText(),
                    original_amount=principal, principal_paid=paid,
                    interest_rate=rate, compounding=compounding,
                    term_months=term, payment=payment, extra_payment=extra,
                    start_date=start, payment_day=pay_day, track_mode=track,
                    interest_source=interest_src, payment_account_id=pay_acct,
                    interest_category_id=interest_cat,
                )
                self.created_account_id = acct_id
                if self._goal_cb.isChecked() and self._has_budget:
                    budgets = self._repo.list_budgets()
                    if budgets:
                        self._repo.create_loan_paydown_goal(acct_id, budgets[0].id)
            else:
                self._repo.update_loan(
                    self._account_id,
                    interest_rate=rate, compounding=compounding,
                    term_months=term,
                    payment=payment, extra_payment=extra,
                    start_date=start, payment_day=pay_day, track_mode=track,
                    interest_source=interest_src, payment_account_id=pay_acct,
                    interest_category_id=interest_cat,
                )
                self.created_account_id = self._account_id
                # Rename the account if it changed (keep currency + opening
                # balance — the latter is fixed by the original amount).
                acct = next(
                    (a for a in self._repo.list_accounts(include_closed=True)
                     if a.id == self._account_id), None,
                )
                if acct is not None and name != acct.name:
                    self._repo.update_account(
                        self._account_id, name=name, currency=acct.currency,
                        opening_balance=acct.opening_balance,
                    )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not save loan", str(e))
            return
        self.accept()

    # ── parse helpers ──

    def _dec(self, w: QLineEdit) -> Optional[Decimal]:
        s = (w.text() or "").strip().replace(",", "").lstrip("$£€")
        if not s:
            return None
        try:
            return Decimal(s)
        except InvalidOperation:
            return None

    def _float(self, w: QLineEdit) -> Optional[float]:
        d = self._dec(w)
        return float(d) if d is not None else None

    def _int(self, w: QLineEdit) -> Optional[int]:
        s = (w.text() or "").strip()
        if not s.isdigit():
            return None
        return int(s)
