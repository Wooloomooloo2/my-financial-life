"""Undo an import (ADR-118).

Lists recent import batches and lets the user reverse one — deleting exactly the
transactions that batch created (splits cascade). Used to recover from an import
that went in wrong (e.g. before a parser fix) so it can be re-imported cleanly.

Rows the import merged into a pre-existing manual transaction aren't created by
the batch and so aren't removed; the dialog says so.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from mfl_desktop.db.repository import Repository

_HEADERS = ("Imported", "Account", "File", "Transactions")


class ImportHistoryDialog(QDialog):
    """Pick a past import and undo it. ``any_undone`` is True if at least one
    batch was reversed, so the caller can refresh the register/sidebar."""

    def __init__(self, repo: Repository, *, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.any_undone = False
        self.setWindowTitle("Undo Import")
        self.setModal(True)
        self.resize(720, 420)

        intro = QLabel(
            "Undo reverses an import, deleting the transactions it added (and "
            "their split lines). Transactions that the import merged into rows "
            "you'd already entered by hand are left in place."
        )
        intro.setWordWrap(True)

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._table.doubleClicked.connect(lambda *_: self._undo_selected())

        self._undo_btn = QPushButton("Undo Selected Import…")
        self._undo_btn.clicked.connect(self._undo_selected)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.addButton(self._undo_btn, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self._table, stretch=1)
        layout.addWidget(buttons)

        self._reload()

    def _reload(self) -> None:
        batches = self._repo.list_import_batches()
        self._batch_ids: list[int] = []
        self._table.setRowCount(len(batches))
        for r, b in enumerate(batches):
            self._batch_ids.append(b["id"])
            live = b.get("live_count", 0)
            cells = (
                (b.get("imported_at") or "")[:19],
                b.get("account_name") or f"account {b.get('account_id')}",
                b.get("source_filename") or b.get("source_format") or "",
                f"{live:,}",
            )
            for c, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if c == 3:
                    item.setTextAlignment(int(Qt.AlignRight | Qt.AlignVCenter))
                self._table.setItem(r, c, item)
        self._undo_btn.setEnabled(bool(batches))

    def _undo_selected(self) -> None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._batch_ids):
            QMessageBox.information(
                self, "Pick an import", "Select an import row to undo.",
            )
            return
        batch_id = self._batch_ids[row]
        file_item = self._table.item(row, 2)
        count_item = self._table.item(row, 3)
        label = file_item.text() if file_item else f"batch {batch_id}"
        count = count_item.text() if count_item else "?"
        confirm = QMessageBox.warning(
            self, "Undo this import?",
            f"Delete the {count} transaction(s) imported from “{label}”?\n\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            n = self._repo.delete_import_batch(batch_id)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(
                self, "Undo failed", f"Could not undo the import:\n\n{e}",
            )
            return
        self.any_undone = True
        QMessageBox.information(
            self, "Import undone",
            f"Removed {n:,} transaction(s). You can now re-import the file.",
        )
        self._reload()
