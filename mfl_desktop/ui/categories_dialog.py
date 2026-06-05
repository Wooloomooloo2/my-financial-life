"""Modal dialog for managing the category tree.

Five operations, mirroring the rename / merge / delete shape of the payee
dialog with reparent layered on top:

- **New**     — add a category under any chosen parent (or top-level).
  Top-level categories pick their kind (income / expense / transfer);
  sub-categories inherit silently.
- **Rename**  — change a single category's name. Sibling-name collisions
  reject; use Merge instead.
- **Reparent** — move a single category to a new parent. Cycles reject;
  sibling-name collisions at the target reject. Cross-kind reparents
  pop an explicit confirmation (ADR-014) — confirming cascades the new
  kind down the subtree.
- **Change Kind** — directly change a category's kind. Cascades to
  descendants. Allowed at any level; when the new kind would differ from
  the parent's kind, a warning is shown so the user knows the tree will
  be visibly mixed (and can reconcile via Reparent if they want).
- **Merge**   — re-point transactions from 2+ source categories onto a
  target. Target may be one of the selected categories or a brand-new
  top-level name typed at the picker (mirrors the payee dialog). Sources
  with subcategories reject. Cross-kind merges reject up front — the
  user converts kind first via reparent.
- **Delete**  — remove a category. Reassigns its transactions to
  Uncategorised. Rejects Uncategorised itself and any category that has
  subcategories.

Emits `categories_changed` after any successful CRUD operation so the
register window can refresh transaction rows whose category_name may have
changed, plus the category combo delegate and the filter-bar combo.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mfl_desktop.db.repository import CATEGORY_KINDS, CategoryNode, Repository

# id assigned to the reserved Uncategorised row in 0001_initial.sql.
UNCATEGORISED_ID = 1
DEFAULT_NEW_KIND = "expense"


class CategoriesDialog(QDialog):
    categories_changed = Signal()

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Categories")
        self.setModal(True)
        self.resize(680, 600)

        # ── widgets ──

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter…")
        self._search.textChanged.connect(self._apply_filter)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Name", "Used in", "Kind", "Source"])
        self._tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tree.setSortingEnabled(False)  # we sort on insert for predictability
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._tree.itemSelectionChanged.connect(self._update_button_state)

        self._new_btn = QPushButton("&New…")
        self._rename_btn = QPushButton("&Rename…")
        self._reparent_btn = QPushButton("Re&parent…")
        self._change_kind_btn = QPushButton("Change &Kind…")
        self._merge_btn = QPushButton("&Merge…")
        self._delete_btn = QPushButton("&Delete")
        self._new_btn.clicked.connect(self._on_new)
        self._rename_btn.clicked.connect(self._on_rename)
        self._reparent_btn.clicked.connect(self._on_reparent)
        self._change_kind_btn.clicked.connect(self._on_change_kind)
        self._merge_btn.clicked.connect(self._on_merge)
        self._delete_btn.clicked.connect(self._on_delete)

        action_row = QHBoxLayout()
        action_row.addWidget(self._new_btn)
        action_row.addStretch(1)
        action_row.addWidget(self._rename_btn)
        action_row.addWidget(self._reparent_btn)
        action_row.addWidget(self._change_kind_btn)
        action_row.addWidget(self._merge_btn)
        action_row.addWidget(self._delete_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        search_row.addWidget(self._search, stretch=1)

        layout = QVBoxLayout(self)
        layout.addLayout(search_row)
        layout.addWidget(self._tree)
        layout.addLayout(action_row)
        layout.addWidget(buttons)

        self._reload_tree()
        self._update_button_state()

    # ── tree population ──

    def _reload_tree(self) -> None:
        nodes = self._repo.list_category_tree()
        self._nodes_by_id: dict[int, CategoryNode] = {n.id: n for n in nodes}
        children_of: dict[Optional[int], list[CategoryNode]] = {}
        for n in nodes:
            children_of.setdefault(n.parent_id, []).append(n)
        for siblings in children_of.values():
            siblings.sort(key=lambda c: c.name.lower())

        self._tree.clear()

        def add_subtree(parent_widget, parent_id: Optional[int]) -> None:
            for node in children_of.get(parent_id, []):
                item = QTreeWidgetItem(
                    [node.name, str(node.usage_count), node.kind, node.source]
                )
                item.setData(0, Qt.UserRole, node.id)
                item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                if parent_widget is None:
                    self._tree.addTopLevelItem(item)
                else:
                    parent_widget.addChild(item)
                add_subtree(item, node.id)

        add_subtree(None, None)
        self._tree.expandAll()
        self._apply_filter(self._search.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()

        def walk(item: QTreeWidgetItem) -> bool:
            """Return True if `item` or any descendant matches the filter."""
            self_matches = (not needle) or (needle in item.text(0).lower())
            any_child_matches = False
            for i in range(item.childCount()):
                child_matches = walk(item.child(i))
                any_child_matches = any_child_matches or child_matches
            visible = self_matches or any_child_matches
            item.setHidden(not visible)
            return visible

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i))

    def _update_button_state(self) -> None:
        ids = self._selected_ids()
        self._rename_btn.setEnabled(len(ids) == 1)
        self._reparent_btn.setEnabled(len(ids) == 1)
        self._change_kind_btn.setEnabled(len(ids) >= 1)
        self._change_kind_btn.setToolTip(
            "Change the kind (income / expense / transfer) on the selected "
            "categories. Cascades to subcategories. If applied to a "
            "sub-category whose new kind differs from its parent, the "
            "tree will show mixed kinds — you can reconcile via Reparent."
        )
        self._merge_btn.setEnabled(len(ids) >= 2)
        self._delete_btn.setEnabled(len(ids) >= 1)

    def _selected_ids(self) -> list[int]:
        ids: list[int] = []
        for item in self._tree.selectedItems():
            cid = item.data(0, Qt.UserRole)
            if isinstance(cid, int):
                ids.append(cid)
        return ids

    def _path_for(self, cid: int) -> str:
        """Full breadcrumb path from root to this node, joined with ' → '.
        Falls back to the leaf name (or id=…) if the chain can't be walked."""
        parts: list[str] = []
        current_id: Optional[int] = cid
        seen: set[int] = set()
        while current_id is not None and current_id not in seen:
            seen.add(current_id)
            node = self._nodes_by_id.get(current_id)
            if node is None:
                break
            parts.append(node.name)
            current_id = node.parent_id
        if not parts:
            return f"id={cid}"
        return " → ".join(reversed(parts))

    # ── parent picker helper ──

    def _build_parent_choices(
        self, exclude_ids: set[int] = frozenset(),
    ) -> list[tuple[Optional[int], str]]:
        """Return (id, indented label) tuples for the New / Reparent pickers.

        `exclude_ids` should contain the node being moved plus all of its
        descendants — otherwise the user could pick an invalid parent.
        Top-level is offered as id=None with label '(top level)'."""
        nodes = list(self._nodes_by_id.values())
        children_of: dict[Optional[int], list[CategoryNode]] = {}
        for n in nodes:
            children_of.setdefault(n.parent_id, []).append(n)
        for siblings in children_of.values():
            siblings.sort(key=lambda c: c.name.lower())

        result: list[tuple[Optional[int], str]] = [(None, "(top level)")]

        def walk(parent_id: Optional[int], depth: int) -> None:
            for node in children_of.get(parent_id, []):
                if node.id in exclude_ids:
                    continue
                result.append((node.id, ("    " * depth) + node.name))
                walk(node.id, depth + 1)

        walk(None, 0)
        return result

    # ── actions ──

    def _on_new(self) -> None:
        # Default parent: the currently-selected node, if any. Otherwise top.
        selected = self._selected_ids()
        default_parent: Optional[int] = selected[0] if len(selected) == 1 else None

        picker = QDialog(self)
        picker.setWindowTitle("New Category")
        picker.setModal(True)
        parent_combo = QComboBox()
        for cid, label in self._build_parent_choices():
            parent_combo.addItem(label, userData=cid)
        if default_parent is not None:
            for i in range(parent_combo.count()):
                if parent_combo.itemData(i) == default_parent:
                    parent_combo.setCurrentIndex(i)
                    break
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("e.g. Dining out")
        kind_combo = QComboBox()
        for k in CATEGORY_KINDS:
            kind_combo.addItem(k, userData=k)

        def sync_kind_for_parent() -> None:
            """When a parent is chosen, lock the kind to the parent's kind
            (subcategories inherit). When (top level) is chosen, unlock so
            the user can pick."""
            pid = parent_combo.currentData()
            if pid is None:
                kind_combo.setEnabled(True)
                kind_combo.setToolTip("New top-level categories choose their own kind.")
                # Default to expense for fresh top-level — most common case.
                self._set_combo_value(kind_combo, DEFAULT_NEW_KIND)
            else:
                node = self._nodes_by_id.get(pid)
                if node is not None:
                    self._set_combo_value(kind_combo, node.kind)
                kind_combo.setEnabled(False)
                kind_combo.setToolTip("Sub-categories inherit their parent's kind.")

        parent_combo.currentIndexChanged.connect(lambda _: sync_kind_for_parent())
        sync_kind_for_parent()

        form = QFormLayout()
        form.addRow("Parent:", parent_combo)
        form.addRow("Name:", name_edit)
        form.addRow("Kind:", kind_combo)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(picker.accept)
        bb.rejected.connect(picker.reject)
        lay = QVBoxLayout(picker)
        lay.addLayout(form)
        lay.addWidget(bb)
        picker.resize(400, picker.sizeHint().height())
        if picker.exec() != QDialog.Accepted:
            return
        name = name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required", "Enter a category name.")
            return
        parent_id = parent_combo.currentData()
        kind = kind_combo.currentData() or DEFAULT_NEW_KIND
        try:
            self._repo.create_category(name, parent_id, kind=kind, source="user")
        except ValueError as e:
            QMessageBox.warning(self, "Could not create category", str(e))
            return
        self._reload_tree()
        self.categories_changed.emit()

    @staticmethod
    def _set_combo_value(combo: QComboBox, value: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    def _on_rename(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        cid = ids[0]
        current = self._nodes_by_id.get(cid)
        if current is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename Category", "New name:", QLineEdit.Normal, current.name,
        )
        if not ok or new_name.strip() == current.name:
            return
        try:
            self._repo.rename_category(cid, new_name)
        except ValueError as e:
            QMessageBox.warning(self, "Could not rename", str(e))
            return
        self._reload_tree()
        self.categories_changed.emit()

    def _on_reparent(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        cid = ids[0]
        node = self._nodes_by_id.get(cid)
        if node is None:
            return
        # Exclude the node and its descendants — those are invalid parents.
        forbidden = self._repo.category_descendants(cid)

        picker = QDialog(self)
        picker.setWindowTitle(f"Reparent {node.name!r}")
        picker.setModal(True)
        label = QLabel(f"Move {node.name!r} under which parent?")
        label.setWordWrap(True)
        combo = QComboBox()
        for pid, plabel in self._build_parent_choices(exclude_ids=forbidden):
            combo.addItem(plabel, userData=pid)
        # Default selection: the node's current parent (or top level).
        for i in range(combo.count()):
            if combo.itemData(i) == node.parent_id:
                combo.setCurrentIndex(i)
                break
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(picker.accept)
        bb.rejected.connect(picker.reject)
        lay = QVBoxLayout(picker)
        lay.addWidget(label)
        lay.addWidget(combo)
        lay.addWidget(bb)
        picker.resize(400, picker.sizeHint().height())
        if picker.exec() != QDialog.Accepted:
            return
        new_parent_id = combo.currentData()
        if new_parent_id == node.parent_id:
            return

        # Detect a cross-kind reparent: if the new parent's kind differs from
        # the moved category's current kind, explicit confirmation is required
        # (ADR-014). Reparenting to top-level (new_parent_id is None) leaves
        # kind unchanged.
        new_kind: Optional[str] = None
        if new_parent_id is not None:
            new_parent = self._nodes_by_id.get(new_parent_id)
            if new_parent is not None and new_parent.kind != node.kind:
                msg = (
                    f"Moving {node.name!r} under {new_parent.name!r} also "
                    f"changes its kind from {node.kind} → {new_parent.kind}. "
                    f"This applies to {node.name!r} and all of its "
                    f"subcategories, and reports will treat those "
                    f"transactions as {new_parent.kind} going forward.\n\n"
                    f"Continue?"
                )
                confirm = QMessageBox.question(
                    self, "Confirm kind change", msg,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if confirm != QMessageBox.Yes:
                    return
                new_kind = new_parent.kind
        try:
            self._repo.reparent_category(cid, new_parent_id, new_kind=new_kind)
        except ValueError as e:
            QMessageBox.warning(self, "Could not reparent", str(e))
            return
        self._reload_tree()
        self.categories_changed.emit()

    def _on_change_kind(self) -> None:
        """Edit the kind on the selected categories. Cascades to descendants.

        Allowed at any level. When the new kind would differ from a selected
        sub-category's parent, the user is warned that the tree will show
        mixed kinds and can reconcile later via Reparent — surfaced so the
        consequence is visible rather than silent."""
        ids = self._selected_ids()
        if not ids:
            return
        nodes = [self._nodes_by_id.get(c) for c in ids]
        nodes = [n for n in nodes if n is not None]
        if not nodes:
            return

        current_kinds = sorted({n.kind for n in nodes})
        current_label = current_kinds[0] if len(current_kinds) == 1 else "(mixed)"

        picker = QDialog(self)
        picker.setWindowTitle("Change Kind")
        picker.setModal(True)
        target_label = (
            nodes[0].name if len(nodes) == 1
            else f"{len(nodes)} categories"
        )
        label = QLabel(
            f"Change kind on {target_label}.\n"
            f"Current: {current_label}"
        )
        label.setWordWrap(True)
        combo = QComboBox()
        for k in CATEGORY_KINDS:
            combo.addItem(k, userData=k)
        if len(current_kinds) == 1:
            self._set_combo_value(combo, current_kinds[0])
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(picker.accept)
        bb.rejected.connect(picker.reject)
        lay = QVBoxLayout(picker)
        lay.addWidget(label)
        lay.addWidget(combo)
        lay.addWidget(bb)
        picker.resize(360, picker.sizeHint().height())
        if picker.exec() != QDialog.Accepted:
            return
        new_kind = combo.currentData()
        if not new_kind:
            return

        # Two consequence checks rolled into one confirmation:
        #   - cascade: any selected category with descendants will have
        #     them all updated too;
        #   - drift: any selected sub-category whose new kind differs from
        #     its parent's kind will leave the tree visibly mixed.
        has_descendants = any(
            self._repo.category_has_children(n.id) for n in nodes
        )
        drifting = [
            n for n in nodes
            if n.parent_id is not None
            and self._nodes_by_id.get(n.parent_id) is not None
            and self._nodes_by_id[n.parent_id].kind != new_kind
        ]
        warnings: list[str] = []
        if has_descendants:
            warnings.append(
                f"Every sub-category under the selection will also become "
                f"kind={new_kind}, and reports will treat their "
                f"transactions as {new_kind} going forward."
            )
        if drifting:
            names = ", ".join(repr(n.name) for n in drifting[:3])
            extra = "" if len(drifting) <= 3 else f" (+{len(drifting) - 3} more)"
            warnings.append(
                f"The new kind differs from the parent's kind for "
                f"{names}{extra} — your tree will show mixed kinds. You "
                f"can reconcile this later via Reparent."
            )
        if warnings:
            body = "\n\n".join(warnings) + "\n\nContinue?"
            confirm = QMessageBox.question(
                self, "Confirm kind change", body,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return

        try:
            for n in nodes:
                self._repo.change_category_kind(n.id, new_kind)
        except ValueError as e:
            QMessageBox.warning(self, "Could not change kind", str(e))
            return
        self._reload_tree()
        self.categories_changed.emit()

    def _on_merge(self) -> None:
        ids = self._selected_ids()
        if len(ids) < 2:
            return
        # ADR-014: merging categories of different kinds is rejected — the
        # user converts kind first via reparent. Surface this before the
        # picker so they don't have to pick a target only to be told no.
        kinds = {
            self._nodes_by_id[c].kind for c in ids
            if c in self._nodes_by_id
        }
        if len(kinds) > 1:
            QMessageBox.warning(
                self, "Different kinds",
                "The selected categories aren't all the same kind "
                f"({', '.join(sorted(kinds))}). Reparent the odd ones under "
                "a matching root first, then try again.",
            )
            return
        shared_kind = next(iter(kinds), DEFAULT_NEW_KIND)
        resolved = self._prompt_merge_target(ids, shared_kind)
        if resolved is None:
            return
        target_id, target_name, created_new = resolved
        sources = [c for c in ids if c != target_id]
        if not sources:
            return
        new_clause = (
            "  (A new top-level category will be created and the selected "
            "categories merged into it.)\n\n"
            if created_new else ""
        )
        confirm = QMessageBox.question(
            self, "Confirm merge",
            f"Merge {len(sources)} categories into {target_name!r}?\n\n"
            f"{new_clause}"
            f"Their transactions will be reassigned and the merged-from "
            f"categories will be deleted. Categories with subcategories "
            f"cannot be merged — reparent or merge those first.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            if created_new:
                # Roll back the orphan target we created up front.
                try:
                    self._repo.delete_category(target_id)
                except Exception:
                    pass
            return
        try:
            moved = self._repo.merge_categories(sources, target_id)
        except ValueError as e:
            QMessageBox.warning(self, "Merge failed", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Merge failed", str(e))
            return
        self._reload_tree()
        self.categories_changed.emit()
        QMessageBox.information(
            self, "Merged",
            f"{moved:,} transaction{'s' if moved != 1 else ''} reassigned "
            f"to {target_name!r}.",
        )

    def _prompt_merge_target(
        self, ids: list[int], shared_kind: str,
    ) -> Optional[tuple[int, str, bool]]:
        """Same shape as the payee dialog: pick from the selected categories,
        or type a brand-new top-level name. A typed name that matches an
        existing top-level category outside the selection is rejected so a
        single Merge never silently absorbs categories the user did not
        choose (see ADR-012 / ADR-013).

        Combo labels show each selected category's *full path* (e.g.
        ``Expense → Other``) so when sources span different parents the
        user can see where each candidate target lives — and where the
        merged result will end up."""
        items = sorted(
            [(cid, self._path_for(cid)) for cid in ids
             if cid in self._nodes_by_id],
            key=lambda t: t[1].lower(),
        )
        selected_by_path = {path: cid for cid, path in items}

        picker = QDialog(self)
        picker.setWindowTitle("Choose merge target")
        picker.setModal(True)
        label = QLabel(
            "Merge into which category? Pick one of the selected categories — "
            "the full path is shown so you can tell apart same-named "
            "categories under different parents — or type a new name to "
            "merge them all into a brand-new top-level category."
        )
        label.setWordWrap(True)
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        for cid, path in items:
            combo.addItem(path, userData=cid)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(picker.accept)
        bb.rejected.connect(picker.reject)
        lay = QVBoxLayout(picker)
        lay.addWidget(label)
        lay.addWidget(combo)
        lay.addWidget(bb)
        picker.resize(480, picker.sizeHint().height())
        if picker.exec() != QDialog.Accepted:
            return None

        typed = combo.currentText().strip()
        if not typed:
            QMessageBox.warning(picker, "Target required", "Pick a name.")
            return None

        # Pick from the combo: typed text matches one of the displayed paths.
        if typed in selected_by_path:
            return (selected_by_path[typed], typed, False)

        # Free-typed target — treat as a brand-new top-level category.
        existing_id = self._repo.find_top_level_category_id_by_name(typed)
        if existing_id is not None and existing_id not in selected_by_path.values():
            QMessageBox.warning(
                picker, "Category not in selection",
                f"A top-level category named {typed!r} already exists but "
                f"isn't in your current selection. Cancel and add it to the "
                f"selection if you want to merge into it.",
            )
            return None
        try:
            new_id = self._repo.create_category(
                typed, parent_id=None, kind=shared_kind, source="user",
            )
        except ValueError as e:
            QMessageBox.warning(picker, "Could not create category", str(e))
            return None
        return (new_id, typed, True)

    def _on_delete(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        # Sanity-check each up front so we can summarise the action before
        # touching anything; the Repository methods also enforce these.
        for cid in ids:
            if cid == UNCATEGORISED_ID:
                QMessageBox.warning(
                    self, "Cannot delete",
                    "The Uncategorised category is the reserved fallback "
                    "and cannot be deleted.",
                )
                return
            if self._repo.category_has_children(cid):
                name = self._nodes_by_id.get(cid)
                label = name.name if name else f"id={cid}"
                QMessageBox.warning(
                    self, "Cannot delete",
                    f"{label!r} has subcategories. Reparent or delete them "
                    f"first.",
                )
                return

        total_txns = sum(self._repo.count_category_transactions(c) for c in ids)
        if len(ids) == 1:
            label = self._nodes_by_id.get(ids[0])
            name = label.name if label else f"id={ids[0]}"
            body = f"Delete category {name!r}?"
        else:
            body = f"Delete {len(ids)} categories?"
        if total_txns > 0:
            body += (
                f"\n\n{total_txns:,} transaction"
                f"{'s' if total_txns != 1 else ''} currently use "
                f"{'this category' if len(ids) == 1 else 'these categories'} "
                f"and will be re-categorised as 'Uncategorised'."
            )
        confirm = QMessageBox.warning(
            self, "Confirm delete", body,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            for cid in ids:
                self._repo.delete_category(cid)
        except ValueError as e:
            QMessageBox.warning(self, "Delete failed", str(e))
        except Exception as e:
            QMessageBox.critical(self, "Delete failed", str(e))
        self._reload_tree()
        self.categories_changed.emit()
