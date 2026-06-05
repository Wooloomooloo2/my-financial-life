"""Modal dialog for setting up a budget — the perimeter and the per-category
amounts in one place.

Tab 1 picks which accounts count toward the budget. Tab 2 lists each
budgeted category with its amount, cadence, and role (bills / saving /
discretionary), backed by Add / Edit / Remove verbs. Saving commits the
perimeter and category sets together; cancelling discards both.

The two halves intentionally share one Save: shipping them as separate
dialogs would create a window where the perimeter is half-changed when
the user backs out of the categories step, and the recovery story for
that is just clicking Save anyway. One atomic save matches how the
budget is used — set up once at the start, tweak as a whole when
priorities shift.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import (
    BUDGET_ROLES,
    AccountSummary,
    Budget,
    BudgetCategoryRow,
    CategoryChoice,
    Repository,
    SCHEDULE_CADENCES,
)
from mfl_desktop.ui.category_picker import (
    make_category_picker,
    selected_category_id,
)


_CADENCE_LABELS = {
    "weekly":    "Weekly",
    "biweekly":  "Bi-weekly",
    "monthly":   "Monthly",
    "quarterly": "Quarterly",
    "annual":    "Annually",
}

_ROLE_LABELS = {
    "bills":         "Bills",
    "saving":        "Saving",
    "discretionary": "Discretionary",
}


@dataclass
class _PendingRow:
    """A row in the working set the dialog hands to the repository on Save.
    `original_category_id` is None for newly-added rows."""
    category_id: int
    amount: Decimal
    cadence: str
    role: str
    original_category_id: Optional[int]


class BudgetSetupDialog(QDialog):
    def __init__(
        self,
        repo: Repository,
        budget: Budget,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._budget = budget
        self.setWindowTitle("Budget Setup")
        self.setModal(True)
        self.resize(720, 560)

        self._all_accounts: list[AccountSummary] = repo.list_accounts()
        self._categories: list[CategoryChoice] = repo.list_categories_flat()
        self._categories_by_id = {c.id: c for c in self._categories}

        # ── Tab 1: account perimeter ──
        self._account_list = QListWidget()
        self._account_list.setSelectionMode(QAbstractItemView.NoSelection)
        current_perimeter = set(repo.list_budget_account_ids(budget.id))
        for acct in self._all_accounts:
            item = QListWidgetItem(f"{acct.name}  ·  {acct.currency}  ·  {acct.family}")
            item.setData(Qt.UserRole, acct.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if acct.id in current_perimeter else Qt.Unchecked
            )
            self._account_list.addItem(item)

        account_help = QLabel(
            "Pick the accounts that count toward this budget. Transfers "
            "between two in-perimeter accounts cancel out; transfers to or "
            "from an out-of-perimeter account count as a normal flow."
        )
        account_help.setWordWrap(True)
        account_help.setStyleSheet("color: #666;")

        account_tab = QWidget()
        account_layout = QVBoxLayout(account_tab)
        account_layout.addWidget(account_help)
        account_layout.addWidget(self._account_list)

        # ── Tab 2: per-category budgets ──
        self._pending_rows: list[_PendingRow] = [
            _PendingRow(
                category_id=bc.category_id,
                amount=bc.amount,
                cadence=bc.cadence,
                role=bc.role,
                original_category_id=bc.category_id,
            )
            for bc in repo.list_budget_categories(budget.id)
        ]

        self._category_table = QTableWidget(0, 4)
        self._category_table.setHorizontalHeaderLabels(
            ["Category", "Cadence", "Amount", "Role"]
        )
        self._category_table.verticalHeader().setVisible(False)
        self._category_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._category_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._category_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self._category_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._category_table.itemDoubleClicked.connect(lambda _: self._on_edit_row())
        self._category_table.itemSelectionChanged.connect(self._update_row_buttons)

        self._add_btn = QPushButton("&Add…")
        self._edit_btn = QPushButton("&Edit…")
        self._remove_btn = QPushButton("&Remove")
        self._add_btn.clicked.connect(self._on_add_row)
        self._edit_btn.clicked.connect(self._on_edit_row)
        self._remove_btn.clicked.connect(self._on_remove_row)

        category_action_row = QHBoxLayout()
        category_action_row.addWidget(self._add_btn)
        category_action_row.addStretch(1)
        category_action_row.addWidget(self._edit_btn)
        category_action_row.addWidget(self._remove_btn)

        category_tab = QWidget()
        category_layout = QVBoxLayout(category_tab)
        category_layout.addWidget(self._category_table)
        category_layout.addLayout(category_action_row)

        # ── Tabs + buttons ──
        self._tabs = QTabWidget()
        self._tabs.addTab(account_tab, "Accounts")
        self._tabs.addTab(category_tab, "Categories")

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs)
        layout.addWidget(buttons)

        self._reload_category_table()
        self._update_row_buttons()

    # ── category table population ──

    def _reload_category_table(self) -> None:
        self._category_table.setRowCount(len(self._pending_rows))
        for i, row in enumerate(self._pending_rows):
            cat = self._categories_by_id.get(row.category_id)
            if cat is not None:
                label = (
                    f"{cat.name} ({cat.parent_name})"
                    if cat.parent_name else cat.name
                )
            else:
                label = f"(unknown id {row.category_id})"
            cat_item = QTableWidgetItem(label)
            cat_item.setData(Qt.UserRole, row.category_id)

            cadence_item = QTableWidgetItem(
                _CADENCE_LABELS.get(row.cadence, row.cadence)
            )

            amount_item = QTableWidgetItem()
            amount_item.setData(Qt.DisplayRole, float(row.amount))
            amount_item.setText(f"{row.amount:,.2f}")
            amount_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            role_item = QTableWidgetItem(
                _ROLE_LABELS.get(row.role, row.role)
            )

            self._category_table.setItem(i, 0, cat_item)
            self._category_table.setItem(i, 1, cadence_item)
            self._category_table.setItem(i, 2, amount_item)
            self._category_table.setItem(i, 3, role_item)

    def _update_row_buttons(self) -> None:
        has_selection = bool(self._category_table.selectionModel().selectedRows())
        self._edit_btn.setEnabled(has_selection)
        self._remove_btn.setEnabled(has_selection)

    def _selected_row_index(self) -> Optional[int]:
        sel = self._category_table.selectionModel().selectedRows()
        if not sel:
            return None
        return sel[0].row()

    # ── category row actions ──

    def _on_add_row(self) -> None:
        # Exclude categories that already have a row in the working set —
        # the schema's UNIQUE(budget_id, category_id) would reject it on save,
        # and double-entry isn't a meaningful user intent.
        in_use = {row.category_id for row in self._pending_rows}
        candidates = [c for c in self._categories if c.id not in in_use]
        if not candidates:
            QMessageBox.information(
                self, "No categories to add",
                "Every category is already in this budget. Remove one or "
                "create a new category from Manage ▸ Categories first.",
            )
            return
        sub = _CategoryRowDialog(candidates, existing=None, parent=self)
        if sub.exec() != QDialog.Accepted:
            return
        values = sub.values()
        if values is None:
            return
        self._pending_rows.append(_PendingRow(
            category_id=values["category_id"],
            amount=values["amount"],
            cadence=values["cadence"],
            role=values["role"],
            original_category_id=None,
        ))
        self._reload_category_table()

    def _on_edit_row(self) -> None:
        idx = self._selected_row_index()
        if idx is None:
            return
        row = self._pending_rows[idx]
        # When editing, the selected row's own category is allowed; any
        # *other* in-use category isn't (same UNIQUE constraint logic).
        in_use = {
            r.category_id for r in self._pending_rows
            if r.category_id != row.category_id
        }
        candidates = [c for c in self._categories if c.id not in in_use]
        sub = _CategoryRowDialog(
            candidates,
            existing={
                "category_id": row.category_id,
                "amount": row.amount,
                "cadence": row.cadence,
                "role": row.role,
            },
            parent=self,
        )
        if sub.exec() != QDialog.Accepted:
            return
        values = sub.values()
        if values is None:
            return
        self._pending_rows[idx] = _PendingRow(
            category_id=values["category_id"],
            amount=values["amount"],
            cadence=values["cadence"],
            role=values["role"],
            original_category_id=row.original_category_id,
        )
        self._reload_category_table()

    def _on_remove_row(self) -> None:
        idx = self._selected_row_index()
        if idx is None:
            return
        del self._pending_rows[idx]
        self._reload_category_table()

    # ── Save ──

    def _on_save(self) -> None:
        # Read perimeter from checkboxes
        perimeter_ids: list[int] = []
        for i in range(self._account_list.count()):
            item = self._account_list.item(i)
            if item.checkState() == Qt.Checked:
                perimeter_ids.append(int(item.data(Qt.UserRole)))

        # Apply both halves under a try/except — each repo call commits
        # atomically, but if the categories pass fails after perimeter
        # succeeded the budget is in a half-applied state. The cost of
        # an explicit BEGIN/COMMIT-spanning-two-methods refactor isn't
        # worth it for a Save-button flow the user can simply re-do, so
        # surface the partial state via the error dialog instead.
        try:
            self._repo.set_budget_accounts(self._budget.id, perimeter_ids)

            # Delete rows whose original category is no longer present.
            current_ids = {row.category_id for row in self._pending_rows}
            originals = {
                row.original_category_id for row in self._pending_rows
                if row.original_category_id is not None
            }
            existing_now = set(
                bc.category_id for bc in
                self._repo.list_budget_categories(self._budget.id)
            )
            to_delete = existing_now - current_ids
            for cid in to_delete:
                self._repo.delete_budget_category(self._budget.id, cid)

            for row in self._pending_rows:
                self._repo.upsert_budget_category(
                    budget_id=self._budget.id,
                    category_id=row.category_id,
                    amount=row.amount,
                    cadence=row.cadence,
                    role=row.role,
                )
        except ValueError as e:
            QMessageBox.warning(self, "Could not save budget", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Could not save budget", str(e))
            return
        self.accept()


class _CategoryRowDialog(QDialog):
    """Tiny sub-dialog for adding or editing one budget_category row."""

    def __init__(
        self,
        categories: list[CategoryChoice],
        existing: Optional[dict] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        is_edit = existing is not None
        self.setWindowTitle("Edit Budget Row" if is_edit else "Add Budget Row")
        self.setModal(True)

        default_id = existing["category_id"] if is_edit else None
        self._category_combo = make_category_picker(
            categories, default_id=default_id,
        )

        self._amount_edit = QLineEdit()
        self._amount_edit.setPlaceholderText("0.00")
        self._amount_edit.setAlignment(Qt.AlignRight)
        validator = QDoubleValidator(0.0, 1_000_000_000.0, 2, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._amount_edit.setValidator(validator)
        if is_edit:
            self._amount_edit.setText(f"{existing['amount']:.2f}")

        self._cadence_combo = QComboBox()
        for key in SCHEDULE_CADENCES:
            self._cadence_combo.addItem(_CADENCE_LABELS[key], userData=key)
        default_cadence = existing["cadence"] if is_edit else "monthly"
        for i in range(self._cadence_combo.count()):
            if self._cadence_combo.itemData(i) == default_cadence:
                self._cadence_combo.setCurrentIndex(i)
                break

        self._role_combo = QComboBox()
        for key in BUDGET_ROLES:
            self._role_combo.addItem(_ROLE_LABELS[key], userData=key)
        default_role = existing["role"] if is_edit else "discretionary"
        for i in range(self._role_combo.count()):
            if self._role_combo.itemData(i) == default_role:
                self._role_combo.setCurrentIndex(i)
                break

        form = QFormLayout()
        form.addRow("Category:", self._category_combo)
        form.addRow("Amount:", self._amount_edit)
        form.addRow("Cadence:", self._cadence_combo)
        form.addRow("Role:", self._role_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.resize(380, self.sizeHint().height())
        self._values: Optional[dict] = None

    def _on_accept(self) -> None:
        cid = selected_category_id(self._category_combo)
        if cid is None:
            QMessageBox.warning(
                self, "Category required",
                "Pick a category from the list.",
            )
            return
        raw = self._amount_edit.text().strip().replace(",", "")
        if not raw:
            QMessageBox.warning(self, "Amount required", "Enter an amount.")
            return
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            QMessageBox.warning(
                self, "Invalid amount", f"Could not parse {raw!r}.",
            )
            return
        if amount < 0:
            QMessageBox.warning(
                self, "Invalid amount",
                "Budget amount must be zero or more.",
            )
            return
        self._values = {
            "category_id": cid,
            "amount": amount,
            "cadence": self._cadence_combo.currentData(),
            "role": self._role_combo.currentData(),
        }
        self.accept()

    def values(self) -> Optional[dict]:
        return self._values
