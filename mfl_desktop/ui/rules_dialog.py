"""Manage ▸ Rules — auto-categorisation rules (ADR-073, Arc G round 2).

The screen the owner asked for: create / edit / delete pattern rules, plus a
read-only view of the existing payee aliases (which are implicit
"is exactly → payee" rules, managed in the Payees dialog) so the whole
automation picture sits in one place.

After creating or editing a rule the dialog offers to apply it to matching
existing transactions (uncategorised category / unset payee only — never
overwriting), mirroring the G1 memory flow. ``rules_changed`` fires after any
change so the register can reload.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
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

from mfl_desktop.db.repository import Repository, RuleRow
from mfl_desktop.rules_engine import MATCH_FIELDS, MATCHER_KINDS
from mfl_desktop.ui.category_picker import (
    make_category_picker,
    selected_category_id,
)
from mfl_desktop.ui.rule_edit_dialog import RuleEditDialog
from mfl_desktop.ui import tokens


def _when_text(r: RuleRow) -> str:
    field = MATCH_FIELDS.get(r.match_field, r.match_field)
    kind = MATCHER_KINDS.get(r.pattern_kind, r.pattern_kind)
    return f"{field} {kind} “{r.pattern}”"


class RulesDialog(QDialog):
    rules_changed = Signal()

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Rules")
        self.setModal(True)
        self.resize(760, 720)

        self._categories = repo.list_categories_flat()
        self._cat_path = {c.id: (c.path or c.name) for c in self._categories}

        # ── rules table ──
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["When", "Sets payee", "Sets category", "Priority"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(False)
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._update_button_state)
        self._table.itemDoubleClicked.connect(lambda *_: self._on_edit())

        self._new_btn = QPushButton("&New Rule…")
        self._edit_btn = QPushButton("&Edit…")
        self._delete_btn = QPushButton("&Delete")
        self._new_btn.clicked.connect(self._on_new)
        self._edit_btn.clicked.connect(self._on_edit)
        self._delete_btn.clicked.connect(self._on_delete)
        rule_actions = QHBoxLayout()
        rule_actions.addWidget(self._new_btn)
        rule_actions.addStretch(1)
        rule_actions.addWidget(self._edit_btn)
        rule_actions.addWidget(self._delete_btn)

        # ── remembered payee categories (ADR-106) ──
        # The per-payee "default category" memory (ADR-072) is what actually
        # auto-categorises most payees on import / pre-fills on entry — a
        # separate mechanism from the pattern rules above. Surfacing it here
        # so the whole automation picture really is in one place.
        self._memory_table = QTableWidget(0, 2)
        self._memory_table.setHorizontalHeaderLabels(["Payee", "Auto-category"])
        self._memory_table.verticalHeader().setVisible(False)
        self._memory_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._memory_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._memory_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._memory_table.setSortingEnabled(False)
        mh = self._memory_table.horizontalHeader()
        mh.setSectionResizeMode(0, QHeaderView.Stretch)
        mh.setSectionResizeMode(1, QHeaderView.Stretch)
        self._memory_table.setMaximumHeight(200)
        self._memory_table.itemSelectionChanged.connect(self._update_button_state)
        self._memory_table.itemDoubleClicked.connect(
            lambda *_: self._on_memory_edit()
        )

        self._mem_edit_btn = QPushButton("Edit &category…")
        self._mem_forget_btn = QPushButton("&Forget")
        self._mem_edit_btn.clicked.connect(self._on_memory_edit)
        self._mem_forget_btn.clicked.connect(self._on_memory_forget)
        mem_actions = QHBoxLayout()
        mem_actions.addStretch(1)
        mem_actions.addWidget(self._mem_edit_btn)
        mem_actions.addWidget(self._mem_forget_btn)

        # ── aliases (read-only) ──
        self._alias_table = QTableWidget(0, 2)
        self._alias_table.setHorizontalHeaderLabels(["Alias", "→ Payee"])
        self._alias_table.verticalHeader().setVisible(False)
        self._alias_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._alias_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._alias_table.setSortingEnabled(False)
        ah = self._alias_table.horizontalHeader()
        ah.setSectionResizeMode(0, QHeaderView.Stretch)
        ah.setSectionResizeMode(1, QHeaderView.Stretch)
        self._alias_table.setMaximumHeight(180)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Auto-categorisation rules"))
        layout.addWidget(self._table, stretch=3)
        layout.addLayout(rule_actions)
        mem_hdr = QLabel(
            "Remembered payee categories — a payee’s saved auto-category "
            "(also set per payee in Payees…)"
        )
        tokens.themed(mem_hdr, "color: {muted}; margin-top: 8px;")
        layout.addWidget(mem_hdr)
        layout.addWidget(self._memory_table, stretch=2)
        layout.addLayout(mem_actions)
        alias_hdr = QLabel(
            "Payee aliases — implicit “is exactly → payee” rules "
            "(manage in Payees…)"
        )
        tokens.themed(alias_hdr, "color: {muted}; margin-top: 8px;")  # slate-500
        layout.addWidget(alias_hdr)
        layout.addWidget(self._alias_table, stretch=2)
        layout.addWidget(buttons)

        self._rules: list[RuleRow] = []
        self._reload()
        self._update_button_state()

    # ── population ──

    def _reload(self) -> None:
        self._rules = self._repo.list_rules()
        self._table.setRowCount(len(self._rules))
        for i, r in enumerate(self._rules):
            when = QTableWidgetItem(_when_text(r))
            when.setData(Qt.UserRole, r.id)
            payee = QTableWidgetItem(r.set_payee_name or "—")
            cat = QTableWidgetItem(r.set_category_path or "—")
            prio = QTableWidgetItem()
            prio.setData(Qt.DisplayRole, r.priority)
            prio.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 0, when)
            self._table.setItem(i, 1, payee)
            self._table.setItem(i, 2, cat)
            self._table.setItem(i, 3, prio)

        memories = self._repo.list_payee_default_categories()
        self._memory_table.setRowCount(len(memories))
        for i, (pid, pname, cat_id) in enumerate(memories):
            p = QTableWidgetItem(pname)
            p.setData(Qt.UserRole, pid)
            c = QTableWidgetItem(self._cat_path.get(cat_id, f"category {cat_id}"))
            self._memory_table.setItem(i, 0, p)
            self._memory_table.setItem(i, 1, c)

        aliases = self._collect_aliases()
        self._alias_table.setRowCount(len(aliases))
        for i, (alias_name, canon_name) in enumerate(aliases):
            a = QTableWidgetItem(alias_name)
            a.setForeground(Qt.darkGray)
            b = QTableWidgetItem(canon_name)
            b.setForeground(Qt.darkGray)
            self._alias_table.setItem(i, 0, a)
            self._alias_table.setItem(i, 1, b)

    def _collect_aliases(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for p in self._repo.list_payees_with_usage():
            if p.canonical_id is not None:
                out.append((p.name, p.canonical_name or ""))
        out.sort(key=lambda t: (t[1].lower(), t[0].lower()))
        return out

    def _update_button_state(self) -> None:
        has = self._selected_rule() is not None
        self._edit_btn.setEnabled(has)
        self._delete_btn.setEnabled(has)
        has_mem = self._selected_memory() is not None
        self._mem_edit_btn.setEnabled(has_mem)
        self._mem_forget_btn.setEnabled(has_mem)

    def _selected_rule(self) -> RuleRow | None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        rid = self._table.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((r for r in self._rules if r.id == rid), None)

    def _selected_memory(self) -> tuple[int, str] | None:
        """The selected remembered-category row as ``(payee_id, payee_name)``,
        or None when nothing is selected."""
        rows = self._memory_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._memory_table.item(rows[0].row(), 0)
        if item is None:
            return None
        return int(item.data(Qt.UserRole)), item.text()

    # ── actions ──

    def _on_new(self) -> None:
        dlg = RuleEditDialog(self._repo, self._categories, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        vals = dlg.values()
        if vals is None:
            return
        try:
            rule_id = self._repo.create_rule(**vals)
        except ValueError as e:
            QMessageBox.warning(self, "Could not create rule", str(e))
            return
        self._reload()
        self.rules_changed.emit()
        self._offer_retroactive(rule_id, vals)

    def _on_edit(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        dlg = RuleEditDialog(self._repo, self._categories, rule=rule, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        vals = dlg.values()
        if vals is None:
            return
        try:
            self._repo.update_rule(rule.id, **vals)
        except ValueError as e:
            QMessageBox.warning(self, "Could not update rule", str(e))
            return
        self._reload()
        self.rules_changed.emit()
        self._offer_retroactive(rule.id, vals)

    def _on_delete(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        confirm = QMessageBox.warning(
            self, "Confirm delete",
            f"Delete this rule?\n\n{_when_text(rule)}\n\n"
            f"Existing transactions keep whatever payee/category they have; "
            f"only future imports stop using the rule.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._repo.delete_rule(rule.id)
        except Exception as e:
            QMessageBox.critical(self, "Could not delete", str(e))
            return
        self._reload()
        self.rules_changed.emit()

    # ── remembered-category actions (ADR-106) ──

    def _on_memory_edit(self) -> None:
        sel = self._selected_memory()
        if sel is None:
            return
        payee_id, payee_name = sel
        current = self._repo.get_payee_default_category(payee_id)
        picker = QDialog(self)
        picker.setWindowTitle("Auto-category")
        picker.setModal(True)
        label = QLabel(
            f"Automatically categorise transactions from “{payee_name}” as:"
        )
        label.setWordWrap(True)
        combo = make_category_picker(self._categories, default_id=current)
        pbuttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        pbuttons.accepted.connect(picker.accept)
        pbuttons.rejected.connect(picker.reject)
        lay = QVBoxLayout(picker)
        lay.addWidget(label)
        lay.addWidget(combo)
        lay.addWidget(pbuttons)
        picker.resize(440, picker.sizeHint().height())
        if picker.exec() != QDialog.Accepted:
            return
        new_cat = selected_category_id(combo)
        if new_cat is None:
            QMessageBox.warning(
                self, "Pick a category",
                "Choose a category from the list.",
            )
            return
        try:
            self._repo.set_payee_default_category(payee_id, new_cat)
        except Exception as e:
            QMessageBox.critical(self, "Could not save", str(e))
            return
        self._reload()
        self._update_button_state()
        self.rules_changed.emit()

    def _on_memory_forget(self) -> None:
        sel = self._selected_memory()
        if sel is None:
            return
        payee_id, payee_name = sel
        confirm = QMessageBox.question(
            self, "Forget category?",
            f"Stop auto-categorising “{payee_name}”?\n\n"
            f"Existing transactions keep their categories; only future "
            f"imports / entries stop pre-filling.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._repo.set_payee_default_category(payee_id, None)
        except Exception as e:
            QMessageBox.critical(self, "Could not save", str(e))
            return
        self._reload()
        self._update_button_state()
        self.rules_changed.emit()

    def _offer_retroactive(self, rule_id: int, vals: dict) -> None:
        """Ask whether to apply the just-saved rule to matching existing
        transactions (ADR-073 — uncategorised/unset only, never overwrites)."""
        probe = RuleRow(
            id=rule_id,
            pattern=vals["pattern"],
            pattern_kind=vals["pattern_kind"],
            match_field=vals["match_field"],
            set_payee_id=vals["set_payee_id"],
            set_category_id=vals["set_category_id"],
            priority=vals["priority"],
        )
        try:
            n = self._repo.count_txns_matching_rule(probe)
        except Exception:
            return
        if n <= 0:
            return
        ask = QMessageBox.question(
            self, "Apply to existing transactions?",
            f"Apply this rule to {n:,} matching existing transaction"
            f"{'s' if n != 1 else ''}?\n\n"
            f"Only an unset payee or an uncategorised category is filled — "
            f"nothing you've already set is changed.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if ask != QMessageBox.Yes:
            return
        try:
            self._repo.apply_rule_to_existing(probe)
        except Exception as e:
            QMessageBox.critical(self, "Could not apply", str(e))
            return
        self.rules_changed.emit()
