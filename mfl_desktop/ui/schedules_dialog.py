"""Modal dialog for managing scheduled transactions.

Round A of the budget arc (ADR-023). Lists every active schedule with
account / payee / category / estimated / cadence / next-due / auto / var
columns and exposes the four verbs:

- **New Schedule…** opens the schedule dialog in create mode.
- **Edit Schedule…** opens it in edit mode for the one selected row.
- **Post Now** materialises the next occurrence. Variable schedules
  prompt for the actual amount; fixed ones go through a brief confirm.
- **Delete** hard-deletes the selected schedule(s); materialised txns
  are untouched (the schedule is a template, the txn is the truth).

Emits ``schedules_changed`` after any mutation so the register window
can refresh its model (a Post Now materialises a new txn that should
appear in the register immediately) and the sidebar balances.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import date, timedelta
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from mfl_desktop.db.repository import Repository, ScheduledTxnRow
from mfl_desktop.ui.schedule_dialog import ScheduleDialog


_CADENCE_LABELS = {
    "weekly":    "Weekly",
    "biweekly":  "Bi-weekly",
    "monthly":   "Monthly",
    "quarterly": "Quarterly",
    "annual":    "Annually",
}


class SchedulesDialog(QDialog):
    schedules_changed = Signal()

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Schedules")
        self.setModal(True)
        self.resize(900, 560)

        # ── widgets ──

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels([
            "Account", "Payee", "Category", "Estimated",
            "Cadence", "Next due", "Auto", "Var.",
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._update_button_state)
        self._table.itemDoubleClicked.connect(lambda _: self._on_edit())

        self._summary = QLabel("")

        self._new_btn = QPushButton("&New Schedule…")
        self._edit_btn = QPushButton("&Edit…")
        self._post_btn = QPushButton("&Post Now")
        self._delete_btn = QPushButton("&Delete")
        self._new_btn.clicked.connect(self._on_new)
        self._edit_btn.clicked.connect(self._on_edit)
        self._post_btn.clicked.connect(self._on_post_now)
        self._delete_btn.clicked.connect(self._on_delete)

        action_row = QHBoxLayout()
        action_row.addWidget(self._new_btn)
        action_row.addStretch(1)
        action_row.addWidget(self._edit_btn)
        action_row.addWidget(self._post_btn)
        action_row.addWidget(self._delete_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addWidget(self._summary)
        layout.addLayout(action_row)
        layout.addWidget(buttons)

        self._reload_table()
        self._update_button_state()

    # ── population ──

    def _reload_table(self) -> None:
        rows = self._repo.list_scheduled_txns()
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))
        for i, s in enumerate(rows):
            # Column 0 — Account name. Stash the schedule id on the row's
            # name item so the selection helpers can pull it back without
            # tracking a parallel list.
            acct_item = QTableWidgetItem(s.account_name)
            acct_item.setData(Qt.UserRole, s.id)

            # Column 1 — Payee. Blank cell is fine.
            payee_item = QTableWidgetItem(s.payee_name)

            # Column 2 — Category. For transfer-kind schedules, hint at
            # the destination so the row is self-explanatory.
            if s.category_kind == "transfer" and s.transfer_to_account_name:
                cat_text = f"{s.category_name} → {s.transfer_to_account_name}"
            else:
                cat_text = s.category_name
            cat_item = QTableWidgetItem(cat_text)

            # Column 3 — Estimated amount, sign-aware. Sort numerically by
            # storing the float on the item (Qt's default text sort would
            # otherwise put '-1,234' next to '-100' incorrectly).
            amount_item = QTableWidgetItem()
            amount_item.setData(Qt.DisplayRole, float(s.estimated_amount))
            amount_item.setText(
                f"{s.estimated_amount:,.2f}"
                + (" (var.)" if s.variable else "")
            )
            amount_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            # Column 4 — Cadence label.
            cadence_item = QTableWidgetItem(_CADENCE_LABELS[s.cadence])

            # Column 5 — Next due date. ISO sorts naturally as text.
            due_item = QTableWidgetItem(s.next_due_date)

            # Column 6 — Auto-post checkmark. Centred check char keeps the
            # column narrow; sort behaviour is text "✓" vs "" which puts
            # auto-posters together.
            auto_item = QTableWidgetItem("✓" if s.auto_post else "")
            auto_item.setTextAlignment(Qt.AlignCenter)

            # Column 7 — Variable flag.
            var_item = QTableWidgetItem("✓" if s.variable else "")
            var_item.setTextAlignment(Qt.AlignCenter)

            self._table.setItem(i, 0, acct_item)
            self._table.setItem(i, 1, payee_item)
            self._table.setItem(i, 2, cat_item)
            self._table.setItem(i, 3, amount_item)
            self._table.setItem(i, 4, cadence_item)
            self._table.setItem(i, 5, due_item)
            self._table.setItem(i, 6, auto_item)
            self._table.setItem(i, 7, var_item)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(5, Qt.AscendingOrder)
        self._update_summary(rows)

    def _update_summary(self, rows: list[ScheduledTxnRow]) -> None:
        if not rows:
            self._summary.setText("No schedules yet.")
            return
        cutoff = (date.today() + timedelta(days=30)).isoformat()
        due_soon = sum(1 for s in rows if s.next_due_date <= cutoff)
        self._summary.setText(
            f"{len(rows):,} schedule{'s' if len(rows) != 1 else ''} · "
            f"{due_soon} due in the next 30 days"
        )

    def _update_button_state(self) -> None:
        ids = self._selected_ids()
        self._edit_btn.setEnabled(len(ids) == 1)
        self._post_btn.setEnabled(len(ids) == 1)
        self._delete_btn.setEnabled(len(ids) >= 1)

    def _selected_ids(self) -> list[int]:
        out: list[int] = []
        for idx in self._table.selectionModel().selectedRows():
            item = self._table.item(idx.row(), 0)
            if item is None:
                continue
            sid = item.data(Qt.UserRole)
            if isinstance(sid, int):
                out.append(sid)
        return out

    # ── actions ──

    def _on_new(self) -> None:
        accounts = self._repo.list_accounts()
        if not accounts:
            QMessageBox.information(
                self, "No accounts",
                "Create an account before scheduling transactions.",
            )
            return
        dialog = ScheduleDialog(
            accounts=accounts,
            categories=self._repo.list_categories_flat(),
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
        self._reload_table()
        self.schedules_changed.emit()

    def _on_edit(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        existing = self._repo.get_scheduled_txn(ids[0])
        if existing is None:
            return
        accounts = self._repo.list_accounts()
        dialog = ScheduleDialog(
            accounts=accounts,
            categories=self._repo.list_categories_flat(),
            existing=existing,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        if values is None:
            return
        try:
            self._repo.update_scheduled_txn(
                ids[0],
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
            QMessageBox.warning(self, "Could not save schedule", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Could not save schedule", str(e))
            return
        self._reload_table()
        self.schedules_changed.emit()

    def _on_post_now(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        sched = self._repo.get_scheduled_txn(ids[0])
        if sched is None:
            return

        actual: Optional[Decimal] = None
        if sched.variable:
            # Variable amount — prompt for the real number. Direction is fixed
            # by the schedule (sign of estimated_amount); the user enters a
            # positive magnitude and we re-sign it.
            magnitude_default = abs(float(sched.estimated_amount))
            magnitude, ok = QInputDialog.getDouble(
                self, "Actual amount",
                f"Actual amount for {sched.payee_name or sched.category_name} "
                f"on {sched.next_due_date}:",
                magnitude_default, 0.0, 1_000_000_000.0, 2,
            )
            if not ok:
                return
            try:
                actual_magnitude = Decimal(f"{magnitude:.2f}")
            except InvalidOperation:
                QMessageBox.warning(
                    self, "Invalid amount", "Could not parse amount.",
                )
                return
            if actual_magnitude <= 0:
                QMessageBox.warning(
                    self, "Invalid amount",
                    "Amount must be greater than zero.",
                )
                return
            actual = (
                -actual_magnitude if sched.estimated_amount < 0
                else actual_magnitude
            )
        else:
            # Fixed — short confirm so a stray click doesn't post a bill.
            amount_text = f"{sched.estimated_amount:,.2f}"
            if sched.category_kind == "transfer":
                body = (
                    f"Post a transfer of {amount_text} on "
                    f"{sched.next_due_date}?\n\n"
                    f"Source: {sched.account_name}\n"
                    f"Destination: {sched.transfer_to_account_name}\n"
                    f"Category: {sched.category_name}\n\n"
                    f"Both halves of the transfer will be created."
                )
            else:
                body = (
                    f"Post {amount_text} from {sched.account_name} "
                    f"as {sched.category_name} on {sched.next_due_date}?"
                )
            confirm = QMessageBox.question(
                self, "Confirm post", body,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if confirm != QMessageBox.Yes:
                return

        try:
            self._repo.post_scheduled_txn(ids[0], actual_amount=actual)
        except ValueError as e:
            QMessageBox.warning(self, "Could not post", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Could not post", str(e))
            return
        self._reload_table()
        self.schedules_changed.emit()

    def _on_delete(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        if len(ids) == 1:
            body = (
                f"Delete this schedule?\n\n"
                f"Any transactions already posted from it are untouched — "
                f"only the template is removed."
            )
        else:
            body = (
                f"Delete {len(ids)} schedules?\n\n"
                f"Any transactions already posted from them are untouched."
            )
        confirm = QMessageBox.warning(
            self, "Confirm delete", body,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            for sid in ids:
                self._repo.delete_scheduled_txn(sid)
        except Exception as e:
            QMessageBox.critical(self, "Could not delete", str(e))
            return
        self._reload_table()
        self.schedules_changed.emit()
