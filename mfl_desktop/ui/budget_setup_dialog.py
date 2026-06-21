"""Budget setup dialog (ADR-058) — perimeter accounts + envelope lines.

Two tabs in one atomic Save:

- **Accounts** — which accounts make up the budget's perimeter (its available
  pool, and the txns that count as actuals). Picked first per principle 3.
- **Categories** — the envelope lines: which categories are budgeted, each with
  a *role* (bills / saving / discretionary) and a *rollover* policy (carry
  unspent forward or reset monthly). Per-month amounts are NOT set here — they
  live in the matrix (principle 10); but a newly-added line can be **seeded from
  history** (the trailing-12-month average, principle 5), stamped across all
  months on Save.

Save order matters: the perimeter is written first so the history-seed query
(which sums over the perimeter accounts) reflects the just-chosen accounts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from PySide6.QtCore import Qt
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
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import (
    BUDGET_ROLES,
    Budget,
    Repository,
)
from mfl_desktop.ui.category_picker import (
    make_category_picker,
    selected_category_id,
)

_ROLE_LABELS = {
    "bills": "Bills",
    "saving": "Saving",
    "discretionary": "Discretionary",
}


@dataclass
class _PendingLine:
    category_id: int
    label: str
    kind: str                  # income / expense / transfer
    role: str
    rollover: str              # none / accumulate
    seed_from_history: bool
    existing: bool             # already saved (vs newly added this session)
    line_id: Optional[int]     # set when existing
    # ADR-094: a schedule this line should be a *bill* for. Set on lines pulled
    # in from the "Add bills from schedules…" picker (and on existing bills so
    # the marker shows); a new bill line is created via add_bill_line_from_schedule.
    scheduled_txn_id: Optional[int] = None


class BudgetSetupDialog(QDialog):
    """Edit a budget's perimeter + envelope lines. Amounts live in the matrix."""

    def __init__(self, repo: Repository, budget: Budget, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._budget = budget
        self.setWindowTitle(f"Set up budget — {budget.name}")
        self.resize(620, 520)

        self._categories = repo.list_categories_flat()
        # Rolled-up so a top-level group reflects its children's activity.
        self._usage = repo.category_rollup_usage_counts()
        self._parent_map = repo.category_parent_map()

        tabs = QTabWidget()
        tabs.addTab(self._build_accounts_tab(), "Accounts")
        tabs.addTab(self._build_categories_tab(), "Categories")

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(tabs)
        root.addWidget(buttons)

    # ── Accounts tab ──

    def _build_accounts_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Pick the accounts in this budget. Only their transactions count "
            "as actuals (transfers between two in-budget accounts cancel out), "
            "and each one feeds the available pool by its chosen contribution:"
            "\n  • Balance — the account's balance (the usual choice);"
            "\n  • Available credit — a card's limit minus what you owe;"
            "\n  • Excluded — counted for actuals, but not in the pool."
        ))
        contributions = self._repo.list_budget_account_contributions(
            self._budget.id
        )
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["Account", "Pool contribution"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 240)
        # (account_id, check_item, combo, family) — row-aligned with the table.
        self._acct_rows: list[tuple] = []
        for acc in self._repo.list_accounts():
            row = table.rowCount()
            table.insertRow(row)
            in_per = acc.id in contributions
            chk = QTableWidgetItem(f"{acc.name}  ·  {acc.currency}")
            chk.setFlags(
                Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable
            )
            chk.setData(Qt.UserRole, acc.id)
            chk.setCheckState(Qt.Checked if in_per else Qt.Unchecked)
            table.setItem(row, 0, chk)
            combo = QComboBox()
            combo.addItem("Balance", "balance")
            if acc.family == "credit":
                combo.addItem("Available credit", "available_credit")
            combo.addItem("Excluded", "excluded")
            mode = contributions.get(acc.id, "balance")
            mi = combo.findData(mode)
            combo.setCurrentIndex(mi if mi >= 0 else 0)
            combo.setEnabled(in_per)
            table.setCellWidget(row, 1, combo)
            self._acct_rows.append((acc.id, chk, combo, acc.family))
        table.itemChanged.connect(self._on_acct_item_changed)
        self._accounts_table = table
        lay.addWidget(table)
        return w

    def _on_acct_item_changed(self, item) -> None:
        """Enable a row's contribution combo only while its account is ticked."""
        if item.column() != 0:
            return
        for aid, chk, combo, _fam in self._acct_rows:
            if chk is item:
                combo.setEnabled(item.checkState() == Qt.Checked)
                break

    def _checked_accounts(self) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for aid, chk, combo, _fam in self._acct_rows:
            if chk.checkState() == Qt.Checked:
                out.append((aid, combo.currentData()))
        return out

    # ── Categories tab ──

    def _build_categories_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Budgeted categories. Set monthly amounts later in the matrix — "
            "or seed a new one from its last 12 months of spending."
        ))
        self._pending: list[_PendingLine] = []
        for ln in self._repo.list_budget_lines(self._budget.id):
            label = (
                f"{ln.category_name} ({ln.category_parent_name})"
                if ln.category_parent_name else ln.category_name
            )
            self._pending.append(_PendingLine(
                category_id=ln.category_id, label=label, kind=ln.category_kind,
                role=ln.role, rollover=ln.rollover, seed_from_history=False,
                existing=True, line_id=ln.id,
                scheduled_txn_id=ln.scheduled_txn_id,
            ))
        self._removed_line_ids: list[int] = []

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Category", "Role", "Rollover"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self._table.doubleClicked.connect(lambda *_: self._on_edit())
        lay.addWidget(self._table)

        btn_row = QHBoxLayout()
        populate_btn = QPushButton("Populate from history…")
        populate_btn.setToolTip(
            "Pre-tick the top-level categories you've actually used in these "
            "accounts over the last 12 months, seeded from their averages."
        )
        populate_btn.clicked.connect(self._on_populate)
        add_btn = QPushButton("Add…")
        add_btn.clicked.connect(self._on_add)
        bills_btn = QPushButton("Add bills from schedules…")
        bills_btn.setToolTip(
            "Pull your scheduled transactions in as bill envelopes (ADR-094) — "
            "each gets a due date on the schedule and its monthly amounts seeded "
            "from the schedule. The burn-down then projects it and stops once paid."
        )
        bills_btn.clicked.connect(self._on_add_bills)
        edit_btn = QPushButton("Edit…")
        edit_btn.clicked.connect(self._on_edit)
        rm_btn = QPushButton("Remove")
        rm_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(populate_btn)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(bills_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(rm_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self._reload_table()
        return w

    def _reload_table(self) -> None:
        self._table.setRowCount(0)
        for pl in self._pending:
            r = self._table.rowCount()
            self._table.insertRow(r)
            # ADR-094: a clock marker flags a bill (scheduled-backed) line.
            label = f"⏰ {pl.label}" if pl.scheduled_txn_id is not None else pl.label
            self._table.setItem(r, 0, QTableWidgetItem(label))
            role_text = "—" if pl.kind == "income" else _ROLE_LABELS[pl.role]
            self._table.setItem(r, 1, QTableWidgetItem(role_text))
            roll = "Rolls over" if pl.rollover == "accumulate" else "Resets"
            self._table.setItem(r, 2, QTableWidgetItem(roll))

    def _on_add_bills(self) -> None:
        """ADR-094: pull existing schedules into the budget as bill lines. The
        user multi-selects from the schedules not already budgeted; fields are
        seeded from each schedule (and stay adjustable in the matrix afterwards)."""
        candidates = self._repo.list_schedules_not_in_budget(self._budget.id)
        # Don't re-offer schedules already queued as pending bills this session.
        pending_sids = {
            pl.scheduled_txn_id for pl in self._pending
            if pl.scheduled_txn_id is not None
        }
        candidates = [s for s in candidates if s.id not in pending_sids]
        if not candidates:
            QMessageBox.information(
                self, "Add bills from schedules",
                "No schedules left to add — every active expense/transfer "
                "schedule is already in this budget.",
            )
            return
        dlg = _PickSchedulesDialog(candidates, parent=None)
        if dlg.exec() != QDialog.Accepted or not dlg.selected:
            return
        for s in dlg.selected:
            label = (
                f"{s.category_name} ({s.category_parent_name})"
                if getattr(s, "category_parent_name", "") else s.category_name
            )
            self._pending.append(_PendingLine(
                category_id=s.category_id, label=label, kind=s.category_kind,
                role="bills" if s.category_kind == "expense" else "discretionary",
                rollover="accumulate" if s.category_kind == "expense" else "none",
                seed_from_history=False, existing=False, line_id=None,
                scheduled_txn_id=s.id,
            ))
        self._reload_table()

    def _on_add(self) -> None:
        existing_ids = {pl.category_id for pl in self._pending}
        # parent=None: avoid the macOS child-modal→parent close cascade that
        # vanishes the budget window (ADR-058 close-on-save bug).
        dlg = _AddCategoriesDialog(
            self._categories, self._usage, existing_ids, self._parent_map,
            parent=None,
        )
        if dlg.exec() != QDialog.Accepted or not dlg.result_lines:
            return
        self._pending.extend(dlg.result_lines)
        self._reload_table()

    def _on_populate(self) -> None:
        """Open the chooser with the top-level categories you've actually used
        (in the currently-ticked accounts, last 12 months) pre-ticked (ADR-058
        prepopulation). Children roll up into these, so this is a clean start."""
        account_ids = [aid for aid, _mode in self._checked_accounts()]
        if not account_ids:
            QMessageBox.information(
                self, "Pick accounts first",
                "Tick the budget's accounts on the Accounts tab — the "
                "suggestion is based on what you've spent in them.",
            )
            return
        preselect = self._repo.top_level_categories_with_activity(
            account_ids, months=12, as_of=date.today().isoformat(),
        )
        existing_ids = {pl.category_id for pl in self._pending}
        dlg = _AddCategoriesDialog(
            self._categories, self._usage, existing_ids, self._parent_map,
            preselect_ids=preselect, parent=None,
        )
        if dlg.exec() != QDialog.Accepted or not dlg.result_lines:
            return
        self._pending.extend(dlg.result_lines)
        self._reload_table()

    def _on_edit(self) -> None:
        r = self._table.currentRow()
        if r < 0 or r >= len(self._pending):
            return
        dlg = _LineDialog(
            self._categories, set(), parent=None, edit=self._pending[r],
        )
        if dlg.exec() != QDialog.Accepted or dlg.result_line is None:
            return
        self._pending[r] = dlg.result_line
        self._reload_table()

    def _on_remove(self) -> None:
        r = self._table.currentRow()
        if r < 0 or r >= len(self._pending):
            return
        pl = self._pending.pop(r)
        if pl.existing and pl.line_id is not None:
            self._removed_line_ids.append(pl.line_id)
        self._reload_table()

    # ── Save ──

    def _on_save(self) -> None:
        try:
            # 1. Perimeter first — the history seed reads over these accounts.
            self._repo.set_budget_accounts(
                self._budget.id, self._checked_accounts(),
            )
            # 2. Removed lines.
            for line_id in self._removed_line_ids:
                self._repo.delete_budget_line(line_id)
            # 3. Add/update lines; seed newly-added ones from history.
            today = date.today().isoformat()
            first_month = self._budget.months()[0]
            for pl in self._pending:
                # ADR-094: a newly-pulled bill creates a scheduled-backed line
                # with its allocations seeded from the schedule.
                if not pl.existing and pl.scheduled_txn_id is not None:
                    self._repo.add_bill_line_from_schedule(
                        budget_id=self._budget.id,
                        schedule_id=pl.scheduled_txn_id,
                    )
                    continue
                line_id = self._repo.add_budget_line(
                    budget_id=self._budget.id, category_id=pl.category_id,
                    role=pl.role, rollover=pl.rollover,
                )
                if not pl.existing and pl.seed_from_history:
                    avg = self._repo.historical_monthly_average(
                        budget_id=self._budget.id,
                        category_id=pl.category_id, as_of=today,
                    )
                    if avg > 0:
                        self._repo.set_line_allocation(
                            line_id, first_month, avg, scope="all",
                        )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Could not save budget setup", str(e))
            return
        self.accept()


class _AddCategoriesDialog(QDialog):
    """Pick categories to budget from a **nested tree** of all categories
    (ADR-058) — same parent→child hierarchy as everywhere else, ordered
    alphabetically within each parent, each node showing its rolled-up usage
    count. Multi-select; each ticked node becomes a line with the chosen default
    role (expenses) + rollover, seeded from history by default. Already-budgeted
    categories appear greyed and un-tickable so the tree stays whole.
    """

    def __init__(
        self, categories, usage: dict[int, int], existing_ids: set[int],
        parent_map: dict, parent=None, preselect_ids: Optional[set[int]] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add categories to budget")
        self.resize(540, 600)
        self._cat_by_id = {c.id: c for c in categories}
        self.result_lines: list[_PendingLine] = []
        preselect = preselect_ids or set()

        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "Suggested categories are pre-ticked — review and adjust."
            if preselect else
            "Tick the categories to budget. Children roll up into a budgeted "
            "parent, so ticking a top-level group is usually enough."
        ))

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter categories…")
        self._search.textChanged.connect(self._apply_filter)
        root.addWidget(self._search)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._build_tree(categories, usage, existing_ids, parent_map, preselect)
        self._tree.expandAll()
        root.addWidget(self._tree, stretch=1)

        form = QFormLayout()
        self._role_combo = QComboBox()
        for role in BUDGET_ROLES:
            self._role_combo.addItem(_ROLE_LABELS[role], role)
        # Role only means something for expenses; income lines ignore it.
        form.addRow("Default role (expenses):", self._role_combo)
        self._rollover_cb = QCheckBox("Roll unspent forward")
        self._rollover_cb.setChecked(True)
        form.addRow("", self._rollover_cb)
        self._seed_cb = QCheckBox("Seed amounts from last 12 months' average")
        self._seed_cb.setChecked(True)
        form.addRow("", self._seed_cb)
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_tree(
        self, categories, usage, existing_ids, parent_map, preselect,
    ) -> None:
        present = self._cat_by_id
        children: dict[Optional[int], list[int]] = {}
        for cid in present:
            pid = parent_map.get(cid)
            if pid not in present:
                pid = None  # parent missing/archived → treat as a root
            children.setdefault(pid, []).append(cid)

        def name_of(cid: int) -> str:
            c = present[cid]
            return (c.name or "").lower()

        def add(cid: int, parent_item) -> QTreeWidgetItem:
            c = present[cid]
            count = usage.get(cid, 0)
            text = f"{c.name}   ·   {count} txn" + ("s" if count != 1 else "")
            item = (
                QTreeWidgetItem(parent_item) if parent_item is not None
                else QTreeWidgetItem(self._tree)
            )
            item.setData(0, Qt.UserRole, cid)
            item.setData(0, Qt.UserRole + 1, (c.path or c.name or "").lower())
            if cid in existing_ids:
                item.setText(0, f"{c.name}   ·   already budgeted")
                item.setForeground(0, Qt.gray)
                item.setFlags(Qt.ItemIsEnabled)  # visible, not checkable
            else:
                item.setText(0, text)
                item.setFlags(
                    Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
                )
                item.setCheckState(
                    0, Qt.Checked if cid in preselect else Qt.Unchecked
                )
            for child_id in sorted(children.get(cid, []), key=name_of):
                add(child_id, item)
            return item

        for root_id in sorted(children.get(None, []), key=name_of):
            add(root_id, None)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()

        def visit(item) -> bool:
            child_visible = False
            for i in range(item.childCount()):
                if visit(item.child(i)):
                    child_visible = True
            self_match = needle in (item.data(0, Qt.UserRole + 1) or "")
            visible = (not needle) or self_match or child_visible
            item.setHidden(not visible)
            if needle and (self_match or child_visible):
                item.setExpanded(True)
            return visible

        for i in range(self._tree.topLevelItemCount()):
            visit(self._tree.topLevelItem(i))

    def _on_ok(self) -> None:
        role = self._role_combo.currentData()
        rollover = "accumulate" if self._rollover_cb.isChecked() else "none"
        seed = self._seed_cb.isChecked()

        def collect(item) -> None:
            cid = item.data(0, Qt.UserRole)
            if (
                cid is not None
                and bool(item.flags() & Qt.ItemIsUserCheckable)
                and item.checkState(0) == Qt.Checked
            ):
                cat = self._cat_by_id.get(int(cid))
                if cat is not None:
                    self.result_lines.append(_PendingLine(
                        category_id=cat.id, label=cat.path or cat.name,
                        kind=cat.kind,
                        # Role/rollover are expense concepts; income ignores them.
                        role="discretionary" if cat.kind == "income" else role,
                        rollover="none" if cat.kind == "income" else rollover,
                        seed_from_history=seed, existing=False, line_id=None,
                    ))
            for i in range(item.childCount()):
                collect(item.child(i))

        for i in range(self._tree.topLevelItemCount()):
            collect(self._tree.topLevelItem(i))

        if not self.result_lines:
            QMessageBox.information(
                self, "Nothing selected", "Tick at least one category.",
            )
            return
        self.accept()


class _LineDialog(QDialog):
    """Add or edit one envelope line — category + role + rollover + seed."""

    def __init__(
        self,
        categories,
        existing_ids: set[int],
        parent=None,
        edit: Optional[_PendingLine] = None,
    ) -> None:
        super().__init__(parent)
        self._edit = edit
        self._cat_by_id = {c.id: c for c in categories}
        self._existing_ids = existing_ids
        self.result_line: Optional[_PendingLine] = None
        self.setWindowTitle("Edit category" if edit else "Add category")

        form = QFormLayout(self)

        self._picker = make_category_picker(
            categories, default_id=edit.category_id if edit else None,
        )
        if edit is not None:
            self._picker.setEnabled(False)  # category is fixed on edit
        form.addRow("Category:", self._picker)

        # Role is an expense concept (bills / saving / discretionary) — it has
        # no meaning for income, so don't ask for it there.
        edit_kind = edit.kind if edit else "expense"
        if edit_kind == "income":
            self._role_combo = None
        else:
            self._role_combo = QComboBox()
            for role in BUDGET_ROLES:
                self._role_combo.addItem(_ROLE_LABELS[role], role)
            if edit is not None:
                self._role_combo.setCurrentIndex(
                    max(0, list(BUDGET_ROLES).index(edit.role))
                )
            form.addRow("Role:", self._role_combo)

        self._rollover_cb = QCheckBox("Roll unspent forward")
        self._rollover_cb.setChecked(
            (edit.rollover == "accumulate") if edit else True
        )
        form.addRow("", self._rollover_cb)

        self._seed_cb = QCheckBox("Seed amounts from last 12 months' average")
        self._seed_cb.setChecked(edit is None)
        if edit is not None:
            self._seed_cb.setEnabled(False)  # only new lines seed
        form.addRow("", self._seed_cb)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _on_ok(self) -> None:
        if self._edit is not None:
            cat_id = self._edit.category_id
        else:
            cat_id = selected_category_id(self._picker)
            if cat_id is None:
                QMessageBox.warning(self, "Pick a category", "Choose a category.")
                return
            if cat_id in self._existing_ids:
                QMessageBox.warning(
                    self, "Already budgeted",
                    "That category is already a line in this budget.",
                )
                return
        cat = self._cat_by_id.get(cat_id)
        kind = cat.kind if cat else "expense"
        label = (cat.path or cat.name) if cat else str(cat_id)
        role = (
            self._role_combo.currentData()
            if self._role_combo is not None else "discretionary"
        )
        self.result_line = _PendingLine(
            category_id=cat_id, label=label, kind=kind, role=role,
            rollover="accumulate" if self._rollover_cb.isChecked() else "none",
            seed_from_history=self._seed_cb.isChecked() and self._edit is None,
            existing=self._edit.existing if self._edit else False,
            line_id=self._edit.line_id if self._edit else None,
            # Preserve the bill link across an Edit… (ADR-094).
            scheduled_txn_id=self._edit.scheduled_txn_id if self._edit else None,
        )
        self.accept()


class _PickSchedulesDialog(QDialog):
    """Multi-select picker of schedules to pull into the budget as bills
    (ADR-094). Each row is a checkable schedule; ``selected`` holds the chosen
    ``ScheduledTxnRow``s on accept."""

    def __init__(self, schedules, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add bills from schedules")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._schedules = schedules
        self.selected: list = []

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Tick the scheduled transactions to add as bill envelopes. Their "
            "monthly amounts are seeded from the schedule and stay adjustable."
        ))
        self._list = QListWidget()
        for s in schedules:
            mag = abs(s.estimated_amount)
            item = QListWidgetItem(
                f"{s.category_name}  ·  {s.payee_name or '—'}  ·  "
                f"{mag:,.2f}  ·  {s.cadence}"
            )
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, s)
            self._list.addItem(item)
        lay.addWidget(self._list)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _on_ok(self) -> None:
        self.selected = [
            self._list.item(i).data(Qt.UserRole)
            for i in range(self._list.count())
            if self._list.item(i).checkState() == Qt.Checked
        ]
        self.accept()
