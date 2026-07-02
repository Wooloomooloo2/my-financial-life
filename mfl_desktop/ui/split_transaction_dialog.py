"""Split-transaction dialog — create + edit (ADR-051).

A split transaction is one payee / date / account / signed total whose total is
divided across several category lines (each with its own memo and a signed
amount). This is the single edit surface for splits:

  * **New Transaction → Split…** opens it in create mode, pre-filled with the
    header fields and the amount you typed (which becomes the starting total).
  * **Double-clicking a split row** in the register opens it in edit mode,
    seeded from the existing parent + its lines.

The parent ``txn`` row keeps the full signed total (so every balance /
reconciliation / net-worth query is untouched); the lines live in ``txn_split``
and must sum to the total. The dialog enforces that with a live **Unassigned**
figure that has to reach zero before Save enables. Line amounts are signed, so a
−120.00 groceries line plus a +20.00 cashback line net to a −100.00 total.

A split line may be a **transfer** (ADR-051 amendment): pick a transfer-kind
category on a line and a "Transfer to" account picker becomes active for that
line. On save, that line spawns a real partner ``txn`` in the chosen account
(the source balance stays correct via the parent total; the destination balance
moves because of the partner). Same-currency only in v1 — the destination picker
offers only accounts whose currency matches the split's account. A non-transfer
line leaves the picker disabled. The dialog writes through the Repository and
calls ``accept()``; the caller reloads.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDateEdit,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QComboBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import (
    AccountSummary,
    CategoryChoice,
    Repository,
    TransactionRow,
)
from mfl_desktop import txn_status
from mfl_desktop.ui.category_picker import make_category_picker, selected_category_id
from mfl_desktop.ui import tokens
from mfl_desktop.ui.date_widgets import make_date_edit

# Lines table columns.
_COL_CATEGORY = 0
_COL_MEMO = 1
_COL_AMOUNT = 2
_COL_DEST = 3


class SplitTransactionDialog(QDialog):
    """Create or edit one split transaction (cash / bank / credit only)."""

    def __init__(
        self,
        repo: Repository,
        account: AccountSummary,
        categories: list[CategoryChoice],
        *,
        seed: Optional[TransactionRow] = None,
        prefill: Optional[dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        """``seed`` (a split parent ``TransactionRow``) selects edit mode.
        ``prefill`` (create mode, from New Transaction) is an optional dict with
        ``posted_date`` / ``payee_name`` / ``status`` / ``memo`` /
        ``total_amount`` keys used to seed the header."""
        super().__init__(parent)
        self._repo = repo
        self._account = account
        self._seed = seed
        # All categories are selectable, including transfers (ADR-051
        # amendment): a transfer-kind line reveals the per-line destination
        # picker below.
        self._categories = list(categories)
        self._kind_by_id = {c.id: c.kind for c in self._categories}
        # Candidate transfer destinations: same currency, not this account
        # (cross-currency split-line transfers are deferred — see docstring).
        self._dest_accounts = [
            a for a in repo.list_accounts()
            if a.id != account.id and a.currency == account.currency
        ]

        self.setWindowTitle(
            "Edit split transaction" if seed else "New split transaction"
        )
        self.setMinimumWidth(680)

        outer = QVBoxLayout(self)

        # ── header ──
        form = QFormLayout()
        outer.addLayout(form)

        acct_label = QLabel(f"{account.name}  ·  {account.currency}")
        tokens.themed(acct_label, "QLabel { color: {muted_strong}; }")
        form.addRow("Account:", acct_label)

        self._date = make_date_edit()
        form.addRow("Date:", self._date)

        self._payee = QLineEdit()
        self._payee.setPlaceholderText("Optional")
        form.addRow("Payee:", self._payee)

        self._status = QComboBox()
        self._status.addItems(txn_status.labels())
        self._status.setCurrentText(txn_status.label(txn_status.PENDING))
        form.addRow("Status:", self._status)

        self._memo = QLineEdit()
        self._memo.setPlaceholderText("Optional overall note")
        form.addRow("Memo:", self._memo)

        self._total = QLineEdit()
        self._total.setPlaceholderText("signed total, e.g. -80.00")
        self._total.setAlignment(Qt.AlignRight)
        self._total.textChanged.connect(self._recompute)
        form.addRow("Total:", self._total)

        # ── lines table ──
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["Category", "Memo", "Amount", "Transfer to"]
        )
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(_COL_CATEGORY, QHeaderView.Stretch)
        hdr.setSectionResizeMode(_COL_MEMO, QHeaderView.Stretch)
        hdr.setSectionResizeMode(_COL_AMOUNT, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_DEST, QHeaderView.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        outer.addWidget(self._table)

        row_buttons = QHBoxLayout()
        add_btn = QPushButton("Add line")
        add_btn.clicked.connect(lambda: self._add_line())
        remove_btn = QPushButton("Remove line")
        remove_btn.clicked.connect(self._remove_selected_line)
        row_buttons.addWidget(add_btn)
        row_buttons.addWidget(remove_btn)
        row_buttons.addStretch(1)
        self._summary = QLabel("")
        tokens.themed(self._summary, "QLabel { color: {muted_strong}; }")
        row_buttons.addWidget(self._summary)
        outer.addLayout(row_buttons)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        self._buttons.button(QDialogButtonBox.Save).setDefault(True)
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

        # ── seed ──
        if seed is not None:
            self._populate_from_seed(seed)
        elif prefill is not None:
            self._populate_from_prefill(prefill)
        else:
            self._add_line()
        self._recompute()

    # ── population ──

    def _populate_from_seed(self, seed: TransactionRow) -> None:
        try:
            self._date.setDate(QDate.fromString(seed.posted_date, "yyyy-MM-dd"))
        except Exception:
            pass
        self._payee.setText(seed.payee_name or "")
        self._status.setCurrentText(txn_status.label(seed.status or txn_status.PENDING))
        self._memo.setText(seed.memo or "")
        self._total.setText(f"{seed.amount:.2f}")
        lines = self._repo.split_lines_for_txn(seed.id)
        if not lines:
            self._add_line()
        for ln in lines:
            self._add_line(
                category_id=ln.category_id, memo=ln.memo, amount=ln.amount,
                transfer_to_account_id=ln.transfer_to_account_id,
            )

    def _populate_from_prefill(self, prefill: dict) -> None:
        pd = prefill.get("posted_date")
        if pd:
            try:
                self._date.setDate(QDate.fromString(pd, "yyyy-MM-dd"))
            except Exception:
                pass
        self._payee.setText(prefill.get("payee_name", "") or "")
        self._status.setCurrentText(
            txn_status.label(prefill.get("status") or txn_status.PENDING))
        self._memo.setText(prefill.get("memo", "") or "")
        total = prefill.get("total_amount")
        if total is not None:
            self._total.setText(f"{Decimal(total):.2f}")
            # Seed a single Uncategorised line holding the whole total so the
            # common "split this one transaction" flow starts balanced.
            self._add_line(
                category_id=self._repo.uncategorised_id(),
                memo="", amount=Decimal(total),
            )
        else:
            self._add_line()

    # ── lines ──

    def _add_line(
        self,
        category_id: Optional[int] = None,
        memo: str = "",
        amount: Optional[Decimal] = None,
        transfer_to_account_id: Optional[int] = None,
    ) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        combo = make_category_picker(self._categories, default_id=category_id)
        self._table.setCellWidget(row, _COL_CATEGORY, combo)

        memo_edit = QLineEdit(memo or "")
        self._table.setCellWidget(row, _COL_MEMO, memo_edit)

        amount_edit = QLineEdit("" if amount is None else f"{amount:.2f}")
        amount_edit.setAlignment(Qt.AlignRight)
        amount_edit.setPlaceholderText("signed")
        amount_edit.textChanged.connect(self._recompute)
        self._table.setCellWidget(row, _COL_AMOUNT, amount_edit)

        dest = self._make_dest_combo(transfer_to_account_id)
        self._table.setCellWidget(row, _COL_DEST, dest)
        # Toggle the destination picker as the line's category changes. The
        # editable category combo fires currentTextChanged on every keystroke;
        # selected_category_id only resolves once the text exactly matches an
        # item, so a half-typed category reads as non-transfer (picker stays
        # disabled) until a transfer category is actually chosen.
        combo.currentTextChanged.connect(
            lambda _t, c=combo, d=dest: self._sync_dest_enabled(c, d)
        )
        self._sync_dest_enabled(combo, dest)

    def _make_dest_combo(
        self, selected_id: Optional[int] = None,
    ) -> QComboBox:
        """A per-line transfer-destination picker (same-currency accounts only).
        Index 0 is a "—" placeholder meaning no destination (``userData`` None);
        the rest carry account ids."""
        combo = QComboBox()
        combo.addItem("—", userData=None)
        for a in self._dest_accounts:
            combo.addItem(a.name, userData=a.id)
        if selected_id is not None:
            idx = combo.findData(selected_id)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(self._recompute)
        return combo

    def _sync_dest_enabled(
        self, category_combo: QComboBox, dest_combo: QComboBox,
    ) -> None:
        """Enable the destination picker only for a transfer-kind line; clear
        it back to "—" when the line isn't (or is no longer) a transfer."""
        is_transfer = self._line_is_transfer(category_combo)
        dest_combo.setEnabled(is_transfer)
        if not is_transfer and dest_combo.currentIndex() != 0:
            dest_combo.setCurrentIndex(0)
        self._recompute()

    def _line_is_transfer(self, category_combo: QComboBox) -> bool:
        cid = selected_category_id(category_combo)
        return cid is not None and self._kind_by_id.get(cid) == "transfer"

    def _remove_selected_line(self) -> None:
        row = self._table.currentRow()
        if row < 0 and self._table.rowCount() > 0:
            row = self._table.rowCount() - 1
        if row >= 0:
            self._table.removeRow(row)
            self._recompute()

    def _line_amount(self, row: int) -> Optional[Decimal]:
        edit = self._table.cellWidget(row, _COL_AMOUNT)
        return _to_decimal(edit.text()) if edit is not None else None

    # ── live total / remainder ──

    def _recompute(self, *_args) -> None:
        total = _to_decimal(self._total.text())
        assigned = Decimal("0.00")
        all_parse = True
        for row in range(self._table.rowCount()):
            amt = self._line_amount(row)
            if amt is None:
                all_parse = False
            else:
                assigned += amt
        if total is None:
            self._summary.setText("Enter a signed total.")
            self._set_save_enabled(False)
            return
        remainder = total - assigned
        cur = self._account.currency
        self._summary.setText(
            f"Assigned {assigned:,.2f} {cur}  ·  Unassigned {remainder:,.2f} {cur}"
        )
        ok = (
            self._table.rowCount() >= 1
            and all_parse
            and remainder == Decimal("0.00")
            and self._transfer_lines_ok()
        )
        self._set_save_enabled(ok)

    def _transfer_lines_ok(self) -> bool:
        """Every transfer-kind line needs a chosen destination and a non-zero
        amount (a zero transfer is meaningless)."""
        for row in range(self._table.rowCount()):
            combo = self._table.cellWidget(row, _COL_CATEGORY)
            if combo is None or not self._line_is_transfer(combo):
                continue
            dest = self._table.cellWidget(row, _COL_DEST)
            if dest is None or dest.currentData() is None:
                return False
            amt = self._line_amount(row)
            if amt is None or amt == Decimal("0.00"):
                return False
        return True

    def _set_save_enabled(self, enabled: bool) -> None:
        self._buttons.button(QDialogButtonBox.Save).setEnabled(enabled)

    # ── save ──

    def _collect_lines(
        self,
    ) -> Optional[list[tuple[int, str, Decimal, Optional[int]]]]:
        lines: list[tuple[int, str, Decimal, Optional[int]]] = []
        for row in range(self._table.rowCount()):
            combo = self._table.cellWidget(row, _COL_CATEGORY)
            cid = selected_category_id(combo) if combo is not None else None
            if cid is None:
                QMessageBox.warning(
                    self, "Split transaction",
                    f"Pick a category for line {row + 1}.",
                )
                return None
            amt = self._line_amount(row)
            if amt is None:
                QMessageBox.warning(
                    self, "Split transaction",
                    f"Enter a signed amount for line {row + 1}.",
                )
                return None
            memo_edit = self._table.cellWidget(row, _COL_MEMO)
            memo = memo_edit.text().strip() if memo_edit is not None else ""

            # Transfer lines carry a destination account; non-transfer lines
            # never do (any stale selection is ignored).
            dest_id: Optional[int] = None
            if self._line_is_transfer(combo):
                dest = self._table.cellWidget(row, _COL_DEST)
                dest_id = dest.currentData() if dest is not None else None
                if dest_id is None:
                    QMessageBox.warning(
                        self, "Split transaction",
                        f"Pick a destination account for transfer line "
                        f"{row + 1}.",
                    )
                    return None
                if amt == Decimal("0.00"):
                    QMessageBox.warning(
                        self, "Split transaction",
                        f"A transfer line can't be zero (line {row + 1}).",
                    )
                    return None
            lines.append((int(cid), memo, amt, dest_id))
        return lines

    def _on_save(self) -> None:
        total = _to_decimal(self._total.text())
        if total is None:
            QMessageBox.warning(self, "Split transaction", "Enter a signed total.")
            return
        lines = self._collect_lines()
        if lines is None:
            return
        if not lines:
            QMessageBox.warning(
                self, "Split transaction", "Add at least one split line.",
            )
            return
        if sum((ln[2] for ln in lines), Decimal("0.00")) != total:
            QMessageBox.warning(
                self, "Split transaction",
                "The split lines must sum to the total (Unassigned must be 0).",
            )
            return

        posted_date = self._date.date().toString("yyyy-MM-dd")
        payee_name = self._payee.text().strip()
        status = txn_status.key_for_label(self._status.currentText())
        memo = self._memo.text().strip()
        payee_id = self._repo.get_or_create_payee(payee_name) if payee_name else None

        try:
            if self._seed is None:
                self._repo.insert_split_transaction(
                    account_id=self._account.id,
                    posted_date=posted_date,
                    payee_id=payee_id,
                    status=status,
                    memo=memo,
                    total_amount=total,
                    lines=lines,
                    import_hash=None,
                    import_batch_id=None,
                )
                self._repo.commit()
            else:
                self._repo.update_split_transaction(
                    self._seed.id,
                    posted_date=posted_date,
                    payee_id=payee_id,
                    status=status,
                    memo=memo,
                    total_amount=total,
                    lines=lines,
                )
        except Exception as e:  # noqa: BLE001
            self._repo.rollback()
            QMessageBox.critical(
                self, "Could not save split",
                f"The split transaction was not saved:\n\n{e}",
            )
            return
        self.accept()


def _to_decimal(text: str) -> Optional[Decimal]:
    """Parse a lenient signed money string to a 2dp Decimal, or None."""
    s = (text or "").strip().replace(",", "").lstrip("$£€").strip()
    if not s or s in ("-", "+"):
        return None
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
