"""Modal dialog for managing the payee list.

Provides six operations:

- **New** — add a canonical payee with no transactions yet.
- **Rename** — change a single payee's name (canonical or alias). Rejects
  collisions with another existing payee; the user is told to use Merge
  instead.
- **Merge** — pick 2+ payees and a target; sources' aliases are re-pointed
  onto the target, sources' transactions are re-pointed onto the target,
  and then the sources are deleted.
- **Make Alias of…** — pick 1+ payees, choose a canonical target. Sources
  become aliases of the target. Transactions stay pointing at the alias
  row (so historical context survives); typeahead and reports route to
  the canonical (ADR-028 / ADR-029 round 1).
- **Promote to Canonical** — drop the alias link, making the row its
  own canonical again. Used when an alias was set in error or the user
  wants to split it back out.
- **Delete** — remove the selected payees. Transactions referencing them
  have their payee_id set to NULL via the schema's FK rule, so no rows
  are lost. Aliases of a deleted canonical auto-promote to canonical
  (same FK rule, ON DELETE SET NULL on `canonical_id`).

The table renders aliases indented under their canonical with a "↳ "
prefix. Column 2 spells out the relationship ("alias of …") so screen
readers and copy-paste still convey it. Sorting on the Name column is
deliberately off — grouping aliases under their canonical is more
useful than alphabetic order across the whole list. The filter box
still works.

Emits ``payees_changed`` after any successful CRUD so the register
window can refresh transaction rows whose payee_name may have
changed (e.g. a typeahead-suggested name disappearing because the
typo was aliased).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
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
from mfl_desktop.ui.category_picker import make_category_picker, selected_category_id


class PayeesDialog(QDialog):
    payees_changed = Signal()  # emitted after any successful CRUD operation

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Payees")
        self.setModal(True)
        self.resize(720, 560)

        # ADR-072: full category breadcrumb paths for the Auto-category column
        # and picker, keyed by id.
        self._categories = repo.list_categories_flat()
        self._cat_paths = {c.id: (c.path or c.name) for c in self._categories}

        # ── widgets ──

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter…")
        self._search.textChanged.connect(self._apply_filter)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["Name", "Alias of", "Auto-category", "Used in"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # Sorting deliberately off — preserves the canonical→aliases
        # grouping the Repository returns. The filter box covers find.
        self._table.setSortingEnabled(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._update_button_state)

        self._new_btn = QPushButton("&New Payee…")
        self._rename_btn = QPushButton("&Rename…")
        self._merge_btn = QPushButton("&Merge…")
        self._alias_btn = QPushButton("Make &Alias of…")
        self._promote_btn = QPushButton("&Promote to Canonical")
        self._autocat_btn = QPushButton("Auto-&category…")
        self._delete_btn = QPushButton("&Delete")
        self._new_btn.clicked.connect(self._on_new)
        self._rename_btn.clicked.connect(self._on_rename)
        self._merge_btn.clicked.connect(self._on_merge)
        self._alias_btn.clicked.connect(self._on_make_alias)
        self._promote_btn.clicked.connect(self._on_promote)
        self._autocat_btn.clicked.connect(self._on_set_auto_category)
        self._delete_btn.clicked.connect(self._on_delete)

        action_row = QHBoxLayout()
        action_row.addWidget(self._new_btn)
        action_row.addStretch(1)
        action_row.addWidget(self._rename_btn)
        action_row.addWidget(self._merge_btn)
        action_row.addWidget(self._alias_btn)
        action_row.addWidget(self._promote_btn)
        action_row.addWidget(self._autocat_btn)
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

        # Lookup table for selection introspection — keyed by payee id, so
        # button-state logic doesn't have to re-read the table cells.
        self._rows_by_id: dict[int, PayeeRow] = {}

        self._reload_table()
        self._update_button_state()

    # ── table population ──

    def _reload_table(self) -> None:
        """Repopulate the table from the repo and re-apply the current filter.
        Repository returns rows pre-grouped (canonical → its aliases); we
        preserve that order verbatim."""
        rows = self._repo.list_payees_with_usage()
        self._rows_by_id = {p.id: p for p in rows}
        self._table.setRowCount(len(rows))
        for i, p in enumerate(rows):
            is_alias = p.canonical_id is not None
            display_name = ("    ↳ " + p.name) if is_alias else p.name

            name_item = QTableWidgetItem(display_name)
            name_item.setData(Qt.UserRole, p.id)
            if is_alias:
                font = name_item.font()
                font.setItalic(True)
                name_item.setFont(font)

            alias_of_text = (
                f"alias of {p.canonical_name}" if is_alias else ""
            )
            alias_of_item = QTableWidgetItem(alias_of_text)
            if is_alias:
                alias_of_item.setForeground(Qt.darkGray)

            # Auto-category cell (ADR-072) — the canonical's remembered
            # category, shown as a full breadcrumb path. Aliases route through
            # their canonical, so the column is blank on alias rows.
            autocat_text = (
                self._cat_paths.get(p.default_category_id, "")
                if p.default_category_id is not None else ""
            )
            autocat_item = QTableWidgetItem(autocat_text)
            autocat_item.setForeground(Qt.darkGray)

            # Count cell — sort numerically by storing the int on the item.
            count_item = QTableWidgetItem()
            count_item.setData(Qt.DisplayRole, p.usage_count)
            count_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            self._table.setItem(i, 0, name_item)
            self._table.setItem(i, 1, alias_of_item)
            self._table.setItem(i, 2, autocat_item)
            self._table.setItem(i, 3, count_item)
        self._apply_filter(self._search.text())

    def _apply_filter(self, text: str) -> None:
        """Filter on the Name column (Repository-supplied; the table cell
        carries the indent + ↳ prefix which would otherwise foil the match)."""
        needle = text.strip().lower()
        for i in range(self._table.rowCount()):
            name_item = self._table.item(i, 0)
            if name_item is None:
                continue
            payee_id = name_item.data(Qt.UserRole)
            row = self._rows_by_id.get(payee_id) if isinstance(payee_id, int) else None
            haystack = row.name.lower() if row else ""
            self._table.setRowHidden(
                i, bool(needle) and needle not in haystack,
            )

    def _update_button_state(self) -> None:
        ids = self._selected_ids()
        selected = [self._rows_by_id[i] for i in ids if i in self._rows_by_id]
        any_selected = len(selected) >= 1
        single = len(selected) == 1
        multi = len(selected) >= 2
        all_aliases = any_selected and all(
            p.canonical_id is not None for p in selected
        )

        self._rename_btn.setEnabled(single)
        self._merge_btn.setEnabled(multi)
        self._alias_btn.setEnabled(any_selected)
        self._promote_btn.setEnabled(all_aliases)
        self._autocat_btn.setEnabled(single)
        self._delete_btn.setEnabled(any_selected)

    def _selected_ids(self) -> list[int]:
        seen: list[int] = []
        for idx in self._table.selectionModel().selectedRows():
            # Skip filtered-out (hidden) rows: search hides non-matching rows
            # rather than removing them, so a shift-click range over visible
            # rows also selects the hidden rows between them. Acting on those
            # would merge/delete payees the user can't see (ADR-106 follow-up).
            if self._table.isRowHidden(idx.row()):
                continue
            item = self._table.item(idx.row(), 0)
            if item is None:
                continue
            payee_id = item.data(Qt.UserRole)
            if isinstance(payee_id, int):
                seen.append(payee_id)
        return seen

    def _name_for(self, payee_id: int) -> str:
        row = self._rows_by_id.get(payee_id)
        return row.name if row else f"id={payee_id}"

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
        _configure_combo_typeahead(combo)
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

    # ── alias verbs (ADR-029 round 1) ──

    def _on_make_alias(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        resolved = self._prompt_alias_target(ids)
        if resolved is None:
            return
        target_id, target_name, created_new = resolved
        sources = [pid for pid in ids if pid != target_id]
        if not sources:
            return

        confirm = QMessageBox.question(
            self, "Confirm alias",
            f"Make {len(sources)} payee"
            f"{'s' if len(sources) != 1 else ''} an alias of "
            f"{target_name!r}?\n\n"
            f"Existing transactions stay pointing at the alias row, so "
            f"history is preserved. The typeahead and reports will route "
            f"the alias through the canonical from now on.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            # Roll back a brand-new canonical if the user pulled out late.
            if created_new:
                try:
                    self._repo.delete_payees([target_id])
                except Exception:
                    pass
            return

        # Apply one source at a time so a per-row failure (two-level rule
        # violation, etc.) doesn't block the rest. Collect failures and
        # surface them in one summary.
        applied = 0
        failures: list[tuple[str, str]] = []
        for sid in sources:
            try:
                self._repo.set_alias_of(sid, target_id)
                applied += 1
            except ValueError as e:
                failures.append((self._name_for(sid), str(e)))

        self._reload_table()
        self.payees_changed.emit()

        if failures:
            body_lines = [
                f"{applied} payee{'s' if applied != 1 else ''} aliased "
                f"to {target_name!r}.",
                "",
                "Could not alias the following:",
            ]
            body_lines.extend(f"  • {name}: {msg}" for name, msg in failures)
            QMessageBox.warning(
                self, "Some aliases not applied", "\n".join(body_lines),
            )

    def _prompt_alias_target(
        self, source_ids: list[int],
    ) -> Optional[tuple[int, str, bool]]:
        """Ask the user to pick the canonical target.

        Returns ``(target_id, target_name, created_new)`` or None on cancel.
        Picker is an editable combo over the existing canonicals; typing a
        brand-new name creates a new canonical and uses it.

        Rejects:
        - target listed among the sources (alias-to-itself),
        - typed name matching an existing alias (target must be canonical),
        - blank input.
        """
        canonicals = self._repo.list_canonical_payees()
        source_set = set(source_ids)
        # A canonical that's in the selection itself can still be picked
        # as target — the others in the selection become aliases of it.

        picker = QDialog(self)
        picker.setWindowTitle("Make alias of…")
        picker.setModal(True)
        label = QLabel(
            "Choose the canonical payee to alias to. Pick from the list, "
            "or type a brand-new name to create a fresh canonical and "
            "alias the selected payees into it."
        )
        label.setWordWrap(True)
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        for cid, name in canonicals:
            combo.addItem(name, userData=cid)
        _configure_combo_typeahead(combo)
        if combo.lineEdit() is not None:
            combo.lineEdit().setPlaceholderText("Type a canonical name…")
            combo.setCurrentIndex(-1)
            combo.lineEdit().setText("")
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(picker.accept)
        buttons.rejected.connect(picker.reject)
        lay = QVBoxLayout(picker)
        lay.addWidget(label)
        lay.addWidget(combo)
        lay.addWidget(buttons)
        picker.resize(420, picker.sizeHint().height())
        if picker.exec() != QDialog.Accepted:
            return None

        typed = combo.currentText().strip()
        if not typed:
            QMessageBox.warning(picker, "Target required", "Pick a name.")
            return None

        # Existing canonical match (combo-list pick OR typed text matching
        # one).
        by_name = {name: cid for cid, name in canonicals}
        if typed in by_name:
            target_id = by_name[typed]
            if target_id in source_set and len(source_set) == 1:
                QMessageBox.warning(
                    picker, "Can't alias to self",
                    "Pick a different payee to alias to.",
                )
                return None
            return (target_id, typed, False)

        # Not a canonical match. Maybe an existing alias — reject; user
        # must pick the canonical, not another alias.
        existing_any = self._repo.find_payee_id_by_name(typed)
        if existing_any is not None:
            QMessageBox.warning(
                picker, "Pick the canonical",
                f"A payee named {typed!r} exists but it's itself an alias. "
                f"Pick its canonical from the list instead.",
            )
            return None

        # Brand-new canonical.
        try:
            new_id = self._repo.create_payee(typed)
        except ValueError as e:
            QMessageBox.warning(picker, "Could not create payee", str(e))
            return None
        return (new_id, typed, True)

    def _on_promote(self) -> None:
        ids = self._selected_ids()
        rows = [self._rows_by_id[i] for i in ids if i in self._rows_by_id]
        if not rows:
            return
        # Only operate on aliases — the button is already gated by
        # _update_button_state but recheck so a stale selection doesn't
        # surprise us.
        alias_rows = [r for r in rows if r.canonical_id is not None]
        if not alias_rows:
            return

        body = (
            f"Promote {len(alias_rows)} alias"
            f"{'es' if len(alias_rows) != 1 else ''} back to canonical?\n\n"
            f"They will reappear in the typeahead and be treated as "
            f"distinct payees from their former canonical."
        )
        confirm = QMessageBox.question(
            self, "Confirm promote", body,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            for r in alias_rows:
                self._repo.promote_to_canonical(r.id)
        except Exception as e:
            QMessageBox.critical(self, "Could not promote", str(e))
            return
        self._reload_table()
        self.payees_changed.emit()

    # ── auto-category memory (ADR-072) ──

    def _on_set_auto_category(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        payee_id = ids[0]
        name = self._name_for(payee_id)
        current = self._repo.get_payee_default_category(payee_id)

        picker = QDialog(self)
        picker.setWindowTitle("Auto-category")
        picker.setModal(True)
        label = QLabel(
            f"Automatically categorise transactions from “{name}” as:"
        )
        label.setWordWrap(True)
        combo = make_category_picker(self._categories, default_id=current)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        clear_btn = buttons.addButton("Clear", QDialogButtonBox.DestructiveRole)
        cleared = {"flag": False}

        def _on_clear() -> None:
            cleared["flag"] = True
            picker.accept()

        clear_btn.clicked.connect(_on_clear)
        buttons.accepted.connect(picker.accept)
        buttons.rejected.connect(picker.reject)
        lay = QVBoxLayout(picker)
        lay.addWidget(label)
        lay.addWidget(combo)
        lay.addWidget(buttons)
        picker.resize(440, picker.sizeHint().height())
        if picker.exec() != QDialog.Accepted:
            return

        if cleared["flag"]:
            new_cat: Optional[int] = None
        else:
            new_cat = selected_category_id(combo)
            if new_cat is None:
                QMessageBox.warning(
                    self, "Pick a category",
                    "Choose a category from the list, or use Clear.",
                )
                return

        try:
            self._repo.set_payee_default_category(payee_id, new_cat)
        except Exception as e:
            QMessageBox.critical(self, "Could not save", str(e))
            return

        # Offer to back-fill existing uncategorised transactions.
        if new_cat is not None:
            try:
                existing = self._repo.count_uncategorised_for_payee(payee_id)
            except Exception:
                existing = 0
            if existing > 0:
                ask = QMessageBox.question(
                    self, "Apply to existing transactions?",
                    f"Apply this category to {existing:,} existing "
                    f"uncategorised {name} transaction"
                    f"{'s' if existing != 1 else ''}?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
                )
                if ask == QMessageBox.Yes:
                    try:
                        self._repo.apply_default_category_to_uncategorised(
                            payee_id, new_cat,
                        )
                    except Exception as e:
                        QMessageBox.critical(self, "Could not apply", str(e))

        self._reload_table()
        self.payees_changed.emit()

    def _on_delete(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        if len(ids) == 1:
            body = (
                f"Delete payee {self._name_for(ids[0])!r}?\n\n"
                f"Any transactions using this payee will keep their other "
                f"fields and show a blank payee. Aliases of this payee "
                f"(if any) will become canonical."
            )
        else:
            body = (
                f"Delete {len(ids)} payees?\n\n"
                f"Any transactions using these payees will keep their other "
                f"fields and show a blank payee. Aliases of these payees "
                f"(if any) will become canonical."
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


def _configure_combo_typeahead(combo: QComboBox) -> None:
    """Switch an editable QComboBox's auto-created completer from Qt's
    default (InlineCompletion + PrefixMatch — which only highlights the
    first prefix-matching item and hides the rest) to the MFL standard:
    PopupCompletion + contains-match + case-insensitive, with the popup
    sized so up to 5 hits actually fit. Matches the ``PayeeTypeaheadDelegate``
    config in ``delegates.py`` and the category picker in
    ``category_picker.py``."""
    completer = combo.completer()
    if completer is None:
        return
    completer.setCompletionMode(QCompleter.PopupCompletion)
    completer.setFilterMode(Qt.MatchContains)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setMaxVisibleItems(5)
    popup = completer.popup()
    popup.setMinimumWidth(280)
    popup.setMinimumHeight(150)
