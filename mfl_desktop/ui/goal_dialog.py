"""Create / edit a savings or pay-down goal (ADR-058 R4b/R4c).

A goal has a name, a direction (savings = assets toward a larger balance /
pay-down = a liability toward a smaller debt), a reporting currency, a target
balance + date, and **one or more contributing accounts** — each with a
**percentage share** of its balance (so one account can be split across goals).
The target is entered as a positive **magnitude** ("balance" for savings, "still
owing" for a pay-down) and converted to a signed balance for storage, so the
user never types a negative number.

The dialog is pure UI over values; persistence (``add_budget_goal`` /
``update_budget_goal`` / ``delete_budget_goal``) is the window's job, which also
passes the eligible-account candidates and the per-account share already used by
*other* goals (for the >100% warning). Created with ``parent=None`` like the
other budget-window dialogs (macOS cascade-close avoidance, ADR-058 R1).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import AccountSummary, BudgetGoal
from mfl_desktop.ui.date_widgets import make_date_edit

_SAVINGS_FAMILIES = ("cash", "investment")


def _is_savings_candidate(a: AccountSummary) -> bool:
    return (not a.is_liability) and a.family in _SAVINGS_FAMILIES


class GoalDialog(QDialog):
    def __init__(
        self,
        *,
        candidates: list[AccountSummary],
        currencies: list[str],
        base_currency: str,
        share_used: Optional[dict[int, int]] = None,
        goal: Optional[BudgetGoal] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._candidates = candidates
        self._by_id = {a.id: a for a in candidates}
        self._share_used = share_used or {}    # account_id -> bp used by OTHER goals
        self._goal = goal
        self._deleted = False
        self._rows: list[tuple[QComboBox, QDoubleSpinBox, QWidget]] = []
        editing = goal is not None
        self.setWindowTitle("Edit goal" if editing else "New goal")
        self.setModal(True)

        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)

        # ── name ──
        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Retirement, Camper van")
        if editing:
            self._name.setText(goal.name)
        form.addRow("Name:", self._name)

        # ── kind (locked in edit mode — re-baselining a direction is a re-create) ──
        self._kind = goal.kind if editing else "savings"
        if editing:
            form.addRow(
                "Type:",
                QLabel("Savings" if self._kind == "savings" else "Pay-down"),
            )
        else:
            self._savings_radio = QRadioButton("Savings")
            self._paydown_radio = QRadioButton("Pay-down")
            self._savings_radio.setChecked(True)
            grp = QButtonGroup(self)
            grp.addButton(self._savings_radio)
            grp.addButton(self._paydown_radio)
            kind_row = QHBoxLayout()
            kind_row.addWidget(self._savings_radio)
            kind_row.addWidget(self._paydown_radio)
            kind_row.addStretch(1)
            kw = QWidget()
            kw.setLayout(kind_row)
            form.addRow("Type:", kw)
            self._savings_radio.toggled.connect(self._on_kind_changed)

        # ── currency ──
        self._currency = QComboBox()
        opts = list(currencies) or [base_currency]
        if base_currency and base_currency not in opts:
            opts.insert(0, base_currency)
        for c in opts:
            self._currency.addItem(c, c)
        sel = goal.currency if editing else base_currency
        idx = self._currency.findData(sel)
        self._currency.setCurrentIndex(idx if idx >= 0 else 0)
        self._currency.currentIndexChanged.connect(self._on_currency_changed)
        form.addRow("Currency:", self._currency)

        # ── target magnitude ──
        self._target = QDoubleSpinBox()
        self._target.setRange(0.0, 99_999_999.0)
        self._target.setDecimals(2)
        self._target.setGroupSeparatorShown(True)
        if editing:
            self._target.setValue(float(abs(goal.target_amount)))
        self._target_caption = QLabel()
        form.addRow(self._target_caption, self._target)

        # ── target date ──
        self._date = make_date_edit()
        if editing:
            y, m, d = (int(x) for x in goal.target_date.split("-"))
            self._date.setDate(QDate(y, m, d))
        else:
            self._date.setDate(QDate.currentDate().addMonths(12))
        form.addRow("Target date:", self._date)

        # ── accounts (one or more, each a % share of its balance) ──
        root.addWidget(QLabel("Accounts (each contributes a % of its balance):"))
        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(4)
        rows_holder = QWidget()
        rows_holder.setLayout(self._rows_box)
        root.addWidget(rows_holder)
        add_btn = QPushButton("＋ Add account")
        add_btn.clicked.connect(lambda: self._add_account_row())
        root.addWidget(add_btn, alignment=Qt.AlignLeft)

        self._apply_framing()
        # Seed rows: existing links in edit mode, else one empty row.
        if editing and goal.accounts:
            for link in goal.accounts:
                self._add_account_row(link.account_id, link.share_bp / 100.0)
        else:
            self._add_account_row()

        # ── buttons ──
        buttons = QDialogButtonBox()
        ok = buttons.addButton(
            "Save" if editing else "Create", QDialogButtonBox.AcceptRole
        )
        ok.clicked.connect(self._on_accept)
        if editing:
            delete = buttons.addButton("Delete", QDialogButtonBox.DestructiveRole)
            delete.clicked.connect(self._on_delete)
        buttons.addButton(QDialogButtonBox.Cancel).clicked.connect(self.reject)
        root.addWidget(buttons)

    # ── framing ──

    def _candidates_for_kind(self) -> list[AccountSummary]:
        if self._kind == "paydown":
            return [a for a in self._candidates if a.is_liability]
        return [a for a in self._candidates if _is_savings_candidate(a)]

    def _apply_framing(self) -> None:
        ccy = self._currency.currentData() or ""
        self._target.setPrefix(f"{ccy} " if ccy else "")
        if self._kind == "paydown":
            self._target_caption.setText("Pay down until owing:")
            self._target.setToolTip(
                "The balance you want to still owe by the target date. "
                "0 = fully paid off."
            )
        else:
            self._target_caption.setText("Save up to:")
            self._target.setToolTip("The total balance you want to reach.")

    def _on_kind_changed(self, _checked: bool) -> None:
        self._kind = "savings" if self._savings_radio.isChecked() else "paydown"
        self._apply_framing()
        # Switching direction invalidates the chosen accounts — reset to one row.
        while self._rows:
            self._remove_account_row(self._rows[0][2])
        self._add_account_row()

    def _on_currency_changed(self, _idx: int) -> None:
        self._apply_framing()

    # ── account rows ──

    def _add_account_row(
        self, account_id: Optional[int] = None, share_pct: float = 100.0,
    ) -> None:
        row = QFrame()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)

        combo = QComboBox()
        for a in self._candidates_for_kind():
            combo.addItem(f"{a.name}  ·  {a.currency}", a.id)
        if account_id is not None:
            i = combo.findData(account_id)
            if i >= 0:
                combo.setCurrentIndex(i)
        lay.addWidget(combo, 1)

        share = QDoubleSpinBox()
        share.setRange(0.01, 100.0)
        share.setDecimals(2)
        share.setSuffix(" %")
        share.setValue(share_pct)
        share.setFixedWidth(96)
        lay.addWidget(share)

        remove = QPushButton("✕")
        remove.setFixedWidth(32)
        remove.setToolTip("Remove this account")
        remove.clicked.connect(lambda: self._remove_account_row(row))
        lay.addWidget(remove)

        self._rows_box.addWidget(row)
        self._rows.append((combo, share, row))

    def _remove_account_row(self, row: QWidget) -> None:
        for i, (_combo, _share, w) in enumerate(self._rows):
            if w is row:
                self._rows.pop(i)
                break
        self._rows_box.removeWidget(row)
        row.deleteLater()

    # ── accept / validate ──

    def _collected_accounts(self) -> list[tuple[int, int]]:
        """`(account_id, share_bp)` from the rows, in order."""
        out: list[tuple[int, int]] = []
        for combo, share, _w in self._rows:
            aid = combo.currentData()
            if aid is None:
                continue
            out.append((int(aid), int(round(share.value() * 100))))
        return out

    def _on_accept(self) -> None:
        if not self._name.text().strip():
            QMessageBox.warning(self, "Name needed", "Give the goal a name.")
            return
        accounts = self._collected_accounts()
        if not accounts:
            QMessageBox.warning(
                self, "Account needed", "Add at least one account to the goal."
            )
            return
        seen: set[int] = set()
        for aid, _bp in accounts:
            if aid in seen:
                QMessageBox.warning(
                    self, "Duplicate account",
                    "Each account can appear once per goal — use its share % "
                    "to split it.",
                )
                return
            seen.add(aid)
        if self._target.value() <= 0:
            QMessageBox.warning(
                self, "Target needed", "Enter a target balance above zero."
            )
            return
        # Soft warning: an account committing >100% across all goals.
        over: list[str] = []
        for aid, bp in accounts:
            total = bp + self._share_used.get(aid, 0)
            if total > 10000:
                a = self._by_id.get(aid)
                over.append(f"{a.name if a else aid} ({total / 100:.0f}%)")
        if over:
            resp = QMessageBox.question(
                self, "Account over-committed",
                "These accounts are assigned more than 100% across all goals:\n\n"
                + "\n".join(over)
                + "\n\nThat's allowed, but progress may double-count. Save anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
        self.accept()

    def _on_delete(self) -> None:
        self._deleted = True
        self.accept()

    # ── results ──

    def was_deleted(self) -> bool:
        return self._deleted

    def result_name(self) -> str:
        return self._name.text().strip()

    def result_kind(self) -> str:
        return self._kind

    def result_currency(self) -> str:
        return self._currency.currentData()

    def result_target_amount(self) -> Decimal:
        """Signed target balance: a pay-down magnitude is stored negative (a
        debt), a savings target positive."""
        mag = Decimal(str(self._target.value()))
        return -mag if self._kind == "paydown" else mag

    def result_target_date(self) -> str:
        return self._date.date().toString("yyyy-MM-dd")

    def result_accounts(self) -> list[tuple[int, int]]:
        return self._collected_accounts()
