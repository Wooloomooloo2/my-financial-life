"""Per-account statement (reconciliation) history (ADR-040 + amendment).

The surface all three reconciliation entry points open: the register's
Reconcile button, the per-account summary's RECONCILE row, and
Account → Reconcile… (Ctrl+Alt+R). Lists every statement for one account
with its status, period, transaction count, and start/end balances, and
launches :class:`ReconcileWizard` to create / resume / view one.

Status, derived live from :class:`StatementRow` (so an edited-after-close
row shows as out of balance without any stored flag):

  - ✓ Reconciled       — closed and ties out (residual 0)
  - ⚠ Out of balance   — closed but residual ≠ 0 (closed unbalanced, or a
                          reconciled row's amount was changed afterwards)
  - ● In progress      — an open statement, resumable

Emits :pyattr:`statements_changed` after any mutation so the opener can
refresh balances / the summary screen (a close stamps rows Reconciled).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from mfl_desktop.db.repository import AccountSummary, Repository, StatementRow
from mfl_desktop.ui.reconcile_wizard import ReconcileWizard


_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€"}
_MUTED = "#475569"
_GREEN = "#16A34A"
_AMBER = "#D97706"
_RED = "#DC2626"


def _fmt(amount: Decimal, currency: str) -> str:
    sym = _SYMBOL.get(currency, f"{currency} ")
    if amount < 0:
        return f"-{sym}{(-amount):,.2f}"
    return f"{sym}{amount:,.2f}"


def _fmt_date(iso: str) -> str:
    # Windows lacks %-d (pitfall #1) — build the day number by hand.
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b %Y')}"


def _period_label(stmt: StatementRow) -> str:
    return f"{_fmt_date(stmt.start_date)} → {_fmt_date(stmt.end_date)}"


class StatementsWindow(QDialog):
    statements_changed = Signal()

    def __init__(
        self,
        repo: Repository,
        account: AccountSummary,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._account = account
        self.setWindowTitle(f"Statements — {account.name}")
        self.setModal(True)
        self.resize(760, 520)

        header = QLabel(f"<b>{account.name}</b> · {account.currency}")
        header.setTextFormat(Qt.RichText)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Status", "Period", "Transactions", "Start", "End"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._update_button_state)
        self._table.itemDoubleClicked.connect(lambda _: self._on_open())

        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color: {_MUTED};")

        self._new_btn = QPushButton("＋ &New Statement…")
        self._open_btn = QPushButton("&Open…")
        self._delete_btn = QPushButton("&Delete")
        self._new_btn.clicked.connect(self._on_new)
        self._open_btn.clicked.connect(self._on_open)
        self._delete_btn.clicked.connect(self._on_delete)

        action_row = QHBoxLayout()
        action_row.addWidget(self._new_btn)
        action_row.addStretch(1)
        action_row.addWidget(self._open_btn)
        action_row.addWidget(self._delete_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self._table)
        layout.addWidget(self._summary)
        layout.addLayout(action_row)
        layout.addWidget(buttons)

        self._reload_table()
        self._update_button_state()

    # ── population ──

    def _reload_table(self) -> None:
        rows = self._repo.list_statements_for_account(self._account.id)
        self._table.setRowCount(len(rows))
        for i, s in enumerate(rows):
            text, colour = self._status_text(s)
            status_item = QTableWidgetItem(text)
            status_item.setData(Qt.UserRole, s.id)
            status_item.setForeground(QColor(colour))
            self._table.setItem(i, 0, status_item)

            self._table.setItem(i, 1, QTableWidgetItem(_period_label(s)))

            count_item = QTableWidgetItem(str(s.txn_count))
            count_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 2, count_item)

            start_item = QTableWidgetItem(
                _fmt(s.starting_balance, self._account.currency)
            )
            start_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 3, start_item)

            end_item = QTableWidgetItem(
                _fmt(s.ending_balance, self._account.currency)
            )
            end_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 4, end_item)

        self._update_summary(rows)

    def _status_text(self, s: StatementRow) -> tuple[str, str]:
        if s.status == "open":
            return "● In progress", _AMBER
        if s.is_out_of_balance:
            return (
                f"⚠ Out of balance ({_fmt(s.residual, self._account.currency)})",
                _RED,
            )
        return "✓ Reconciled", _GREEN

    def _update_summary(self, rows: list[StatementRow]) -> None:
        if not rows:
            self._summary.setText(
                "No statements yet — start your first reconciliation."
            )
            return
        out = sum(1 for s in rows if s.is_out_of_balance)
        reconciled = sum(
            1 for s in rows
            if s.status == "reconciled" and not s.is_out_of_balance
        )
        open_n = sum(1 for s in rows if s.status == "open")
        parts = [f"{len(rows):,} statement{'s' if len(rows) != 1 else ''}"]
        if reconciled:
            parts.append(f"{reconciled} reconciled")
        if out:
            parts.append(f"{out} out of balance")
        if open_n:
            parts.append(f"{open_n} in progress")
        self._summary.setText(" · ".join(parts))

    def _update_button_state(self) -> None:
        has = self._selected_id() is not None
        self._open_btn.setEnabled(has)
        self._delete_btn.setEnabled(has)

    def _selected_id(self) -> Optional[int]:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._table.item(rows[0].row(), 0)
        if item is None:
            return None
        sid = item.data(Qt.UserRole)
        return int(sid) if isinstance(sid, int) else None

    # ── actions ──

    def _run_wizard(self, statement: Optional[StatementRow]) -> None:
        wiz = ReconcileWizard(
            repo=self._repo,
            account=self._account,
            statement=statement,
            parent=self,
        )
        wiz.exec()
        if wiz.committed:
            self._reload_table()
            self.statements_changed.emit()

    def _on_new(self) -> None:
        existing = self._repo.get_open_statement(self._account.id)
        if existing is not None:
            QMessageBox.information(
                self, "Finish the open statement",
                "This account already has a reconciliation in progress "
                f"(ending {_fmt_date(existing.end_date)}). Opening it so you "
                "can finish or discard it first.",
            )
            self._run_wizard(existing)
            return
        self._run_wizard(None)

    def _on_open(self) -> None:
        sid = self._selected_id()
        if sid is None:
            return
        stmt = self._repo.get_statement(sid)
        if stmt is None:
            return
        self._run_wizard(stmt)

    def _on_delete(self) -> None:
        sid = self._selected_id()
        if sid is None:
            return
        stmt = self._repo.get_statement(sid)
        if stmt is None:
            return
        if stmt.status == "reconciled":
            body = (
                "Delete this statement?\n\n"
                f"The {stmt.txn_count} reconciled transaction"
                f"{'s' if stmt.txn_count != 1 else ''} will revert to "
                "Cleared. The transactions themselves are not deleted."
            )
        else:
            body = (
                "Discard this in-progress reconciliation?\n\n"
                "No transactions are changed."
            )
        confirm = QMessageBox.warning(
            self, "Confirm delete", body,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._repo.delete_statement(sid)
        except Exception as e:  # noqa: BLE001 - surface any failure
            QMessageBox.critical(self, "Could not delete", str(e))
            return
        self._reload_table()
        self._update_button_state()
        self.statements_changed.emit()
