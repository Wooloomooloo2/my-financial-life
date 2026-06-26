"""Modal dialog for reviewing the category import-map (ADR-112).

Whenever the user merges, deletes, or reparents a category, the app records a
**source path → my category** mapping so a later import of that old name
reroutes to the right place instead of recreating the category. This dialog
makes that otherwise-invisible map inspectable: it lists every mapping (the
Banktivity-style source path on the left, the category it now resolves to on the
right) and lets the user **Forget** any that are wrong, so the source path goes
back to resolving normally.

It's intentionally read-mostly — mappings are *created* as a side effect of the
curation verbs in the categories dialog, not hand-authored here. The only
mutation offered is forgetting, which is always safe (it just removes a
redirect).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mfl_desktop.db.repository import Repository


class ImportMappingsDialog(QDialog):
    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Import category mappings")
        self.resize(560, 380)

        intro = QLabel(
            "When you merge, delete, or move a category, the app remembers where "
            "its old name should go so a future import doesn't recreate it. These "
            "are those redirects. Select one and choose Forget to make its source "
            "path import normally again."
        )
        intro.setWordWrap(True)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Imported as", "Goes to"])
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._tree.setAlternatingRowColors(True)
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self._tree.itemSelectionChanged.connect(self._update_button_state)

        self._forget_btn = QPushButton("&Forget")
        self._forget_btn.clicked.connect(self._on_forget)

        self._empty_lbl = QLabel("No mappings yet.")
        self._empty_lbl.setAlignment(Qt.AlignCenter)
        self._empty_lbl.setStyleSheet("color: #94a3b8;")

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        action_row.addWidget(self._forget_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self._tree, stretch=1)
        layout.addWidget(self._empty_lbl)
        layout.addLayout(action_row)
        layout.addWidget(buttons)

        self._reload()

    def _reload(self) -> None:
        self._tree.clear()
        rows = self._repo.list_category_import_map()
        for source_path, _target_id, target_label in rows:
            item = QTreeWidgetItem([source_path, target_label or "—"])
            item.setData(0, Qt.UserRole, source_path)
            self._tree.addTopLevelItem(item)
        self._empty_lbl.setVisible(not rows)
        self._tree.setVisible(bool(rows))
        self._update_button_state()

    def _update_button_state(self) -> None:
        self._forget_btn.setEnabled(bool(self._tree.selectedItems()))

    def _on_forget(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            return
        paths = [it.data(0, Qt.UserRole) for it in items]
        n = len(paths)
        msg = (
            f"Forget {'this mapping' if n == 1 else f'these {n} mappings'}?\n\n"
            "The source path will import normally again — which may recreate the "
            "category if your tree no longer has a match."
        )
        if QMessageBox.question(
            self, "Forget mapping", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        for p in paths:
            self._repo.delete_category_import_mapping(p)
        self._reload()
