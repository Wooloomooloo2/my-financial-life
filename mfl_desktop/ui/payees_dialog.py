"""Modal dialog for managing the payee list.

Provides four operations:

- **New** — add a payee with no transactions yet.
- **Rename** — change a single payee's name. Rejects collisions with another
  existing payee; the user is told to use Merge instead.
- **Merge** — pick 2+ payees and a target; sources are re-pointed onto the
  target's id (so historical transactions follow) and then deleted.
- **Delete** — remove the selected payees. Transactions referencing them
  have their payee_id set to NULL via the schema's FK rule, so no rows
  are lost.

The dialog reloads its own table after every operation and emits
``payees_changed`` so the register window can refresh transaction rows
whose payee_name may have changed.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from mfl_desktop.db.repository import PayeeRow, Repository


class PayeesDialog(QDialog):
    payees_changed = Signal()  # emitted after any successful CRUD operation

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Payees")
        self.setModal(True)
        self.resize(560, 520)

        # ── widgets ──

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter…")
        self._search.textChanged.connect(self._apply_filter)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Name", "Used in"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._update_button_state)

        self._new_btn = QPushButton("&New Payee…")
        self._rename_btn = QPushButton("&Rename…")
        self._merge_btn = QPushButton("&Merge…")
        self._delete_btn = QPushButton("&Delete")
        self._new_btn.clicked.connect(self._on_new)
        self._rename_btn.clicked.connect(self._on_rename)
        self._merge_btn.clicked.connect(self._on_merge)
        self._delete_btn.clicked.connect(self._on_delete)

        action_row = QHBoxLayout()
        action_row.addWidget(self._new_btn)
        action_row.addStretch(1)
        action_row.addWidget(self._rename_btn)
        action_row.addWidget(self._merge_btn)
        action_row.addWidget(self._delete_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        search_row.addWidget(self._search, stretch=1)

        layout = QVBoxLayout(self)
        layout.addLayout(search_row)
        layout.addWidget(self._table)
        layout.addLayout(action_row)
        layout.addWidget(buttons)

        self._reload_table()
        self._update_button_state()

    # ── table population ──

    def _reload_table(self) -> None:
        """Repopulate the table from the repo and re-apply the current filter.
        Sorting is temporarily disabled while rows are inserted, otherwise
        QTableWidget re-sorts after every insert and indices drift."""
        rows = self._repo.list_payees_with_usage()
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))
        for i, p in enumerate(rows):
            name_item = QTableWidgetItem(p.name)
            name_item.setData(Qt.UserRole, p.id)
            # Make the count cell sort numerically by storing the int on the
            # item — Qt's default text sort would otherwise order '142' < '89'.
            count_item = QTableWidgetItem()
            count_item.setData(Qt.DisplayRole, p.usage_count)
            count_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 0, name_item)
            self._table.setItem(i, 1, count_item)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, Qt.AscendingOrder)
        self._apply_filter(self._search.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._table.rowCount()):
            name = self._table.item(i, 0).text().lower()
            self._table.setRowHidden(i, bool(needle) and needle not in name)

    def _update_button_state(self) -> None:
        ids = self._selected_ids()
        self._rename_btn.setEnabled(len(ids) == 1)
        self._merge_btn.setEnabled(len(ids) >= 2)
        self._delete_btn.setEnabled(len(ids) >= 1)

    def _selected_ids(self) -> list[int]:
        seen: list[int] = []
        for idx in self._table.selectionModel().selectedRows():
            item = self._table.item(idx.row(), 0)
            if item is None:
                continue
            payee_id = item.data(Qt.UserRole)
            if isinstance(payee_id, int):
                seen.append(payee_id)
        return seen

    def _name_for(self, payee_id: int) -> str:
        for i in range(self._table.rowCount()):
            item = self._table.item(i, 0)
            if item is not None and item.data(Qt.UserRole) == payee_id:
                return item.text()
        return f"id={payee_id}"

    # ── actions ──

    def _on_new(self) -> None:
        name, ok = QInputDialog.getText(
            self, "New Payee", "Name:", QLineEdit.Normal, "",
        )
        if not ok:
            return
        try:
            self._repo.create_payee(name)
        except ValueError as e:
            QMessageBox.warning(self, "Could not create payee", str(e))
            return
        self._reload_table()
        self.payees_changed.emit()

    def _on_rename(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        payee_id = ids[0]
        current = self._name_for(payee_id)
        new_name, ok = QInputDialog.getText(
            self, "Rename Payee", "New name:", QLineEdit.Normal, current,
        )
        if not ok or new_name.strip() == current:
            return
        try:
            self._repo.rename_payee(payee_id, new_name)
        except ValueError as e:
            QMessageBox.warning(self, "Could not rename", str(e))
            return
        self._reload_table()
        self.payees_changed.emit()

    def _on_merge(self) -> None:
        ids = self._selected_ids()
        if len(ids) < 2:
            return
        resolved = self._prompt_merge_target(ids)
        if resolved is None:
            return
        target_id, target_name, created_new = resolved
        sources = [pid for pid in ids if pid != target_id]
        if not sources:
            # Picked one of the selected payees and nothing else needs merging.
            return
        new_clause = (
            "  (A new payee with this name will be created and the selected "
            "payees merged into it.)\n\n"
            if created_new else ""
        )
        confirm = QMessageBox.question(
            self, "Confirm merge",
            f"Merge {len(sources)} payees into {target_name!r}?\n\n"
            f"{new_clause}"
            f"Their transactions will be reassigned and the merged-from "
            f"payees will be deleted.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            # Roll back the brand-new target row if the user pulled out late;
            # otherwise we'd leave an orphan payee with no transactions.
            if created_new:
                try:
                    self._repo.delete_payees([target_id])
                except Exception:
                    pass
            return
        try:
            moved = self._repo.merge_payees(sources, target_id)
        except Exception as e:
            QMessageBox.critical(self, "Merge failed", str(e))
            return
        self._reload_table()
        self.payees_changed.emit()
        QMessageBox.information(
            self, "Merged",
            f"{moved:,} transaction{'s' if moved != 1 else ''} reassigned "
            f"to {target_name!r}.",
        )

    def _prompt_merge_target(
        self, ids: list[int],
    ) -> Optional[tuple[int, str, bool]]:
        """Ask the user to pick the merge target.

        Returns ``(target_id, target_name, created_new)`` or None if the
        user cancelled. ``created_new`` is True when the target is a
        brand-new payee row that was inserted as part of this prompt
        (so the caller can roll it back if the confirmation is declined).

        The picker is an editable combo seeded with the selected payees:
        the user can pick one of those or type a brand-new name. Typing
        a name that matches an existing payee outside the selection is
        rejected — merging that one would silently pull in a payee the
        user did not choose (see ADR-012)."""
        names = sorted(
            [(pid, self._name_for(pid)) for pid in ids],
            key=lambda t: t[1].lower(),
        )
        selected_by_name = {name: pid for pid, name in names}

        picker = QDialog(self)
        picker.setWindowTitle("Choose merge target")
        picker.setModal(True)
        label = QLabel(
            "Merge into which payee? Pick one of the selected payees, or "
            "type a new name to merge them all into a brand-new payee."
        )
        label.setWordWrap(True)
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        for pid, name in names:
            combo.addItem(name, userData=pid)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(picker.accept)
        buttons.rejected.connect(picker.reject)
        lay = QVBoxLayout(picker)
        lay.addWidget(label)
        lay.addWidget(combo)
        lay.addWidget(buttons)
        picker.resize(380, picker.sizeHint().height())
        if picker.exec() != QDialog.Accepted:
            return None

        typed = combo.currentText().strip()
        if not typed:
            QMessageBox.warning(picker, "Target required", "Pick a name.")
            return None

        if typed in selected_by_name:
            return (selected_by_name[typed], typed, False)

        # Typed something free-form. Could be brand-new, or could collide
        # with a payee that's not in the user's current selection.
        existing_id = self._repo.find_payee_id_by_name(typed)
        if existing_id is not None and existing_id not in selected_by_name.values():
            QMessageBox.warning(
                picker, "Payee not in selection",
                f"A payee named {typed!r} already exists but isn't in your "
                f"current selection. Cancel and add it to the selection if "
                f"you want to merge into it.",
            )
            return None

        try:
            new_id = self._repo.create_payee(typed)
        except ValueError as e:
            QMessageBox.warning(picker, "Could not create payee", str(e))
            return None
        return (new_id, typed, True)

    def _on_delete(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        if len(ids) == 1:
            body = (
                f"Delete payee {self._name_for(ids[0])!r}?\n\n"
                f"Any transactions using this payee will keep their other "
                f"fields and show a blank payee."
            )
        else:
            body = (
                f"Delete {len(ids)} payees?\n\n"
                f"Any transactions using these payees will keep their other "
                f"fields and show a blank payee."
            )
        confirm = QMessageBox.warning(
            self, "Confirm delete", body,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._repo.delete_payees(ids)
        except Exception as e:
            QMessageBox.critical(self, "Could not delete", str(e))
            return
        self._reload_table()
        self.payees_changed.emit()
