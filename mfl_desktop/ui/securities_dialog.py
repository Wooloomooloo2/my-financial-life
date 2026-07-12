"""Manage → Securities… dialog (ADR-044, ADR-052).

The price-management screen for investment holdings. Mirrors the Currencies
dialog (ADR-035):

- the Tiingo API key (with the same "stored inside this .mfl file" disclaimer)
- Refresh Now (synchronous fetch of the latest close for every tickered
  security, with a small wait cursor)
- last-refresh timestamp
- a table of every security with its latest price (or "—" when unpriced) so
  the user can see at a glance which holdings still need a price
- an "Add manual price" row — the universal fallback, and the only way to price
  the securities that carry no ticker (most of a Banktivity export)

All writes go through ``Repository.set_setting`` / ``upsert_security_price``.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import Repository, SecurityRow
from mfl_desktop.prices import backfill_historical_into, refresh_latest_prices_into
from mfl_desktop.ui.merge_securities_dialog import MergeSecuritiesDialog
from mfl_desktop.ui.stock_record_dialog import StockRecordDialog
from mfl_desktop.ui import tokens
from mfl_desktop.ui.date_widgets import make_date_edit
from mfl_desktop.ui.secret_field import LockableSecretField


def _fmt_refresh_time(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return "Never"
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    return ts.strftime("%Y-%m-%d %H:%M")


class SecuritiesDialog(QDialog):
    """Securities + prices. Modal but non-blocking on success — Refresh Now and
    Add Manual Price write through immediately so the user can iterate."""

    def __init__(self, repo: Repository, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Securities and prices")
        self.setMinimumWidth(680)
        self.setMinimumHeight(560)

        outer = QVBoxLayout(self)
        outer.setSpacing(14)

        # ── Provider section ──────────────────────────────────────────────
        provider_box = QGroupBox("Tiingo (price provider)")
        prov_layout = QVBoxLayout(provider_box)

        form = QFormLayout()
        # Locked once a token is stored so it can't be edited by accident;
        # the Change button unlocks it (ADR-127). ``_key_edit`` stays pointed at
        # the inner line edit so the refresh/save handlers read it unchanged.
        self._key_field = LockableSecretField(
            placeholder="Paste your free Tiingo API token (see tiingo.com — free signup)",
            value=self._repo.get_setting("tiingo_api_key") or "",
            change_tooltip="Unlock the API-key field to replace your Tiingo token.",
        )
        self._key_edit = self._key_field.line_edit
        form.addRow("API key:", self._key_field)
        prov_layout.addLayout(form)

        disclaimer = QLabel(
            "Stored inside this .mfl file. Remove before sharing snapshots. "
            "Only securities with a ticker symbol can be fetched — price the "
            "rest manually below."
        )
        disclaimer.setWordWrap(True)
        tokens.themed(disclaimer, "QLabel { color: {muted}; font-size: 11px; }")
        prov_layout.addWidget(disclaimer)

        action_row = QHBoxLayout()
        self._refresh_status = QLabel(
            f"Last refresh: "
            f"{_fmt_refresh_time(self._repo.get_setting('tiingo_last_refresh_at'))}"
        )
        tokens.themed(self._refresh_status, "QLabel { color: {muted_strong}; }")
        action_row.addWidget(self._refresh_status, 1)
        self._refresh_btn = QPushButton("Refresh now")
        self._refresh_btn.clicked.connect(self._on_refresh_now)
        action_row.addWidget(self._refresh_btn)
        self._backfill_btn = QPushButton("Backfill history")
        self._backfill_btn.setToolTip(
            "Fetch each tickered security's full daily price history — powers "
            "the portfolio value-over-time chart. One call per ticker."
        )
        self._backfill_btn.clicked.connect(self._on_backfill_history)
        action_row.addWidget(self._backfill_btn)
        prov_layout.addLayout(action_row)

        outer.addWidget(provider_box)

        # ── Prices table ──────────────────────────────────────────────────
        prices_box = QGroupBox("Securities and latest prices")
        prices_layout = QVBoxLayout(prices_box)

        # Filter row — search by symbol/name + a held-only toggle. The list can
        # run to ~90 securities (many carried in from un-migrated accounts), so
        # narrowing to what's actually held is the common case.
        filter_row = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search symbol or name…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(lambda _t: self._apply_filter())
        self._held_only = QCheckBox("Show only held securities")
        self._held_only.setToolTip(
            "Hide securities you no longer hold (fully sold) and ones carried "
            "in from accounts not yet migrated (no transactions)."
        )
        self._held_only.toggled.connect(lambda _c: self._apply_filter())
        # ADR-155: retired securities are hidden from every other read path, so
        # this is the one place they can be seen — and put back.
        self._show_archived = QCheckBox("Show retired")
        self._show_archived.setToolTip(
            "Also list securities you've stopped tracking after closing the "
            "position. Select one and click 'Track again' to restore it."
        )
        self._show_archived.toggled.connect(lambda _c: self._reload_table())
        self._count_label = QLabel("")
        tokens.themed(self._count_label, "QLabel { color: {muted}; }")
        filter_row.addWidget(QLabel("Filter:"))
        filter_row.addWidget(self._search_edit, 1)
        filter_row.addWidget(self._held_only)
        filter_row.addWidget(self._show_archived)
        filter_row.addWidget(self._count_label)
        prices_layout.addLayout(filter_row)

        # Caches populated by _reload_table; the filter renders from these.
        self._all_securities: list[SecurityRow] = []
        self._latest: dict = {}
        self._held_ids: set[int] = set()

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Symbol", "Security", "Price", "As of", "Source"]
        )
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setToolTip(
            "Double-click a security to open its Stock Record "
            "(price history, transactions, ticker)."
        )
        self._table.cellDoubleClicked.connect(self._on_row_double_clicked)
        # Row index → SecurityRow, kept in step with _reload_table.
        self._row_securities: list[SecurityRow] = []
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        prices_layout.addWidget(self._table)

        open_row = QHBoxLayout()
        open_row.addStretch(1)
        # ADR-155: the inverse of the close-out's "stop tracking" prompt.
        self._track_btn = QPushButton("Track again")
        self._track_btn.setToolTip(
            "Restore a retired security: it returns to this list and to the "
            "price refresh."
        )
        self._track_btn.clicked.connect(self._on_track_again)
        self._track_btn.setVisible(False)
        open_row.addWidget(self._track_btn)
        self._merge_btn = QPushButton("Merge…")
        self._merge_btn.setToolTip(
            "Combine the selected security with another record for the same "
            "instrument (e.g. a fund imported under two names, or renamed when "
            "moved between accounts)."
        )
        self._merge_btn.clicked.connect(self._on_merge_securities)
        open_row.addWidget(self._merge_btn)
        self._open_record_btn = QPushButton("Open stock record")
        self._open_record_btn.setToolTip(
            "Open the selected security's Stock Record — price history, "
            "transactions, and ticker editing."
        )
        self._open_record_btn.clicked.connect(self._on_open_stock_record)
        open_row.addWidget(self._open_record_btn)
        prices_layout.addLayout(open_row)

        # Add-manual-price row.
        add_row = QHBoxLayout()
        self._add_security = QComboBox()
        self._add_security.setEditable(True)
        self._add_security.setInsertPolicy(QComboBox.NoInsert)
        self._add_security.completer().setCompletionMode(
            self._add_security.completer().CompletionMode.PopupCompletion
        )
        self._add_security.completer().setCaseSensitivity(Qt.CaseInsensitive)
        for s in self._repo.list_securities():
            label = f"{s.symbol} — {s.name}" if (s.symbol or "").strip() else s.name
            self._add_security.addItem(label, s.id)
        self._add_price = QLineEdit()
        self._add_price.setPlaceholderText("price")
        self._add_price.setMaximumWidth(100)
        self._add_date = make_date_edit()
        self._add_btn = QPushButton("Add price")
        self._add_btn.clicked.connect(self._on_add_manual_price)

        add_row.addWidget(QLabel("Add manual:"))
        add_row.addWidget(self._add_security, 1)
        add_row.addWidget(self._add_price)
        add_row.addWidget(self._add_date)
        add_row.addWidget(self._add_btn)
        prices_layout.addLayout(add_row)

        outer.addWidget(prices_box, 1)

        # ── Buttons ───────────────────────────────────────────────────────
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Save).setDefault(True)
        buttons.accepted.connect(self._on_save_and_close)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._reload_table()

    # ── data refreshers ─────────────────────────────────────────────────

    def _reload_table(self) -> None:
        """Refresh the cached securities, latest prices, and held-set from the
        repository, then render through the current search / held-only filter.
        Called on open and after any write (refresh, backfill, manual price,
        Stock Record edit)."""
        self._all_securities = self._repo.list_securities(
            include_archived=self._show_archived.isChecked(),
        )
        self._latest = self._repo.latest_prices()
        self._held_ids = self._repo.securities_currently_held()
        self._track_btn.setVisible(self._show_archived.isChecked())
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Render the table from the cached data, applying the search needle
        (symbol or name) and the held-only toggle. Cheap — no DB hit — so it's
        safe to call on every keystroke."""
        needle = self._search_edit.text().strip().lower()
        held_only = self._held_only.isChecked()
        visible = [
            s for s in self._all_securities
            if (not held_only or s.id in self._held_ids)
            and (not needle or needle in f"{s.symbol or ''} {s.name}".lower())
        ]
        self._row_securities = visible
        self._table.setRowCount(len(visible))
        for i, s in enumerate(visible):
            self._table.setItem(i, 0, QTableWidgetItem(s.symbol or ""))
            # ADR-155: a retired security is only ever visible here, so say so
            # in the row itself rather than relying on the checkbox for context.
            name = f"{s.name}  (retired)" if s.archived_at else s.name
            self._table.setItem(i, 1, QTableWidgetItem(name))
            price_row = self._latest.get(s.id)
            if price_row is not None:
                price_item = QTableWidgetItem(f"{price_row.price:,.4f}".rstrip("0").rstrip("."))
                price_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._table.setItem(i, 2, price_item)
                self._table.setItem(i, 3, QTableWidgetItem(price_row.price_date))
                self._table.setItem(i, 4, QTableWidgetItem(price_row.source))
            else:
                dash = QTableWidgetItem("—")
                dash.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._table.setItem(i, 2, dash)
                self._table.setItem(i, 3, QTableWidgetItem(""))
                self._table.setItem(i, 4, QTableWidgetItem(""))
        total = len(self._all_securities)
        suffix = "" if held_only else f" · {len(self._held_ids)} held"
        self._count_label.setText(f"Showing {len(visible)} of {total}{suffix}")

    # ── retire / restore (ADR-155) ──────────────────────────────────────

    def _on_track_again(self) -> None:
        """Un-retire the selected security: it returns to the list and to the
        next price refresh. Nothing else changes — retiring only ever gated
        display and pricing."""
        row = self._table.currentRow()
        if not (0 <= row < len(self._row_securities)):
            QMessageBox.information(
                self, "Track again", "Select a retired security first.",
            )
            return
        security = self._row_securities[row]
        if not security.archived_at:
            QMessageBox.information(
                self, "Track again",
                f"{security.symbol or security.name} is already being tracked.",
            )
            return
        self._repo.unarchive_security(security.id)
        self._repo.commit()
        self._reload_table()

    # ── stock record ────────────────────────────────────────────────────

    def _on_row_double_clicked(self, row: int, _column: int) -> None:
        self._open_record_for_row(row)

    def _on_open_stock_record(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Stock record", "Select a security first.",
            )
            return
        self._open_record_for_row(row)

    def _open_record_for_row(self, row: int) -> None:
        if not (0 <= row < len(self._row_securities)):
            return
        security = self._row_securities[row]
        StockRecordDialog(self._repo, security, self).exec()
        # Ticker / prices may have changed — refresh the latest-price view.
        self._reload_table()

    def _on_merge_securities(self) -> None:
        row = self._table.currentRow()
        if not (0 <= row < len(self._row_securities)):
            QMessageBox.information(
                self, "Merge securities", "Select a security to merge first.",
            )
            return
        # The selected row is the security in hand; the dialog lets the user
        # pick the other record (same-ticker matches surfaced first) and choose
        # which survives. Reload either way — a merge drops the absorbed row.
        MergeSecuritiesDialog(self._repo, self._row_securities[row], self).exec()
        self._reload_table()

    # ── button handlers ─────────────────────────────────────────────────

    def _on_refresh_now(self) -> None:
        """Synchronous refresh — persists the key first so the user can paste
        and click in one go."""
        typed_key = self._key_edit.text().strip()
        self._repo.set_setting("tiingo_api_key", typed_key)
        if not typed_key:
            QMessageBox.warning(
                self, "Need an API key",
                "Add your Tiingo API token to the field above before "
                "refreshing. (Securities with no ticker can still be priced "
                "manually below.)",
            )
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            result = refresh_latest_prices_into(self._repo, force=True)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "Refresh failed", f"Could not refresh prices:\n\n{e}",
            )
            return
        QApplication.restoreOverrideCursor()
        # ADR-049 amendment: a security already holding the latest close is
        # skipped without a Tiingo request, so "0 updated" is the *success* case
        # when you're current — say so, rather than leaving it looking like a
        # silent failure.
        skipped_note = (
            f" · {result.skipped_count} already up to date"
            if result.skipped_count else ""
        )
        self._refresh_status.setText(
            f"Last refresh: {_fmt_refresh_time(result.fetched_at)} · "
            f"{result.new_prices_count} price"
            f"{'s' if result.new_prices_count != 1 else ''} updated"
            f"{skipped_note}"
        )
        if result.errors:
            shown = result.errors[:12]
            more = len(result.errors) - len(shown)
            msg = "\n".join(shown) + (f"\n… and {more} more" if more > 0 else "")
            QMessageBox.warning(
                self, "Some prices failed",
                "Refresh finished with errors (often a fund ticker Tiingo "
                f"doesn't cover — price those manually):\n\n{msg}",
            )
        self._reload_table()

    def _on_backfill_history(self) -> None:
        """Fetch full daily history for every tickered security (ADR-045).
        Synchronous with a wait cursor — it's one call per ticker, so a handful
        of seconds for this portfolio."""
        typed_key = self._key_edit.text().strip()
        self._repo.set_setting("tiingo_api_key", typed_key)
        if not typed_key:
            QMessageBox.warning(
                self, "Need an API key",
                "Add your Tiingo API token before backfilling history.",
            )
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            result = backfill_historical_into(self._repo)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "Backfill failed", f"Could not backfill history:\n\n{e}",
            )
            return
        QApplication.restoreOverrideCursor()
        skipped_note = (
            f" · {result.skipped_count} already up to date"
            if result.skipped_count else ""
        )
        if result.new_prices_count == 0 and not result.errors:
            # Nothing needed fetching — every held ticker already has a full,
            # current series, so the click spent no Tiingo requests (ADR-049
            # follow-up). Say so rather than a bare "0 stored".
            self._refresh_status.setText(
                f"History already up to date{skipped_note} — no requests used"
            )
        else:
            self._refresh_status.setText(
                f"Last refresh: {_fmt_refresh_time(result.fetched_at)} · "
                f"{result.new_prices_count:,} historical price"
                f"{'s' if result.new_prices_count != 1 else ''} stored"
                f"{skipped_note}"
            )
        if result.errors:
            shown = result.errors[:12]
            more = len(result.errors) - len(shown)
            msg = "\n".join(shown) + (f"\n… and {more} more" if more > 0 else "")
            QMessageBox.warning(
                self, "Some securities had no history",
                "Backfill finished with errors (often a fund ticker Tiingo "
                f"doesn't cover):\n\n{msg}",
            )
        self._reload_table()

    def _on_add_manual_price(self) -> None:
        idx = self._add_security.currentIndex()
        # When the user typed into the editable combo, currentIndex may not
        # track the typed text — resolve by matching the data for the text.
        sid = self._add_security.itemData(idx)
        if sid is None or self._add_security.currentText() != self._add_security.itemText(idx):
            match = self._add_security.findText(self._add_security.currentText())
            sid = self._add_security.itemData(match) if match >= 0 else None
        if sid is None:
            QMessageBox.warning(
                self, "Add manual price", "Pick a security from the list.",
            )
            return
        text = self._add_price.text().strip().replace(",", "").lstrip("$£€")
        on_date = self._add_date.date().toString("yyyy-MM-dd")
        try:
            price = float(text)
        except ValueError:
            QMessageBox.warning(
                self, "Add manual price", f"{text!r} doesn't look like a number.",
            )
            return
        if price < 0:
            QMessageBox.warning(
                self, "Add manual price", "Price can't be negative.",
            )
            return
        try:
            self._repo.upsert_security_price(
                security_id=int(sid), price_date=on_date,
                price=price, source="manual",
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(
                self, "Add manual price", f"Could not save price:\n\n{e}",
            )
            return
        self._add_price.clear()
        self._reload_table()

    def _on_save_and_close(self) -> None:
        self._repo.set_setting("tiingo_api_key", self._key_edit.text().strip())
        self.accept()
