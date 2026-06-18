"""Manage → Currencies… dialog (ADR-035).

Surfaces the multi-currency state to the user:

- the openexchangerates.org API key (with disclaimer that it's stored
  inside the .mfl file — remove before sharing a snapshot)
- Refresh Now (synchronous fetch with a small wait cursor)
- last-refresh timestamp
- a small table of latest rates for every (USD → quote) pair we've seen
- an "Add manual rate" row at the bottom for ad-hoc entries (and for
  testing matching before paying for openexchangerates)
- the matcher tunables from ADR-036 (window-days, FX tolerance) since
  both ADRs put them on this screen

All writes go through ``Repository.set_setting`` / ``upsert_fx_rate``;
the dialog never mutates state directly.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop.db.repository import Repository
from mfl_desktop.fx import FxFetchError, refresh_latest_into
from mfl_desktop.ui import tokens
from mfl_desktop.ui.date_widgets import make_date_edit


_OXR_SIGNUP_URL = "https://openexchangerates.org/signup/free"


def _fmt_refresh_time(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return "Never"
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    return ts.strftime("%Y-%m-%d %H:%M")


class CurrenciesDialog(QDialog):
    """One screen for the user's currency state.

    Modal but non-blocking on success: closing via OK persists any field
    edits (API key, matcher tunables). Refresh Now and Add Manual Rate
    write through immediately so the user can iterate without having to
    close + reopen.
    """

    def __init__(self, repo: Repository, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Currencies and exchange rates")
        self.setMinimumWidth(620)
        self.setMinimumHeight(520)

        outer = QVBoxLayout(self)
        outer.setSpacing(14)

        # ── Provider section ──────────────────────────────────────────────
        provider_box = QGroupBox("openexchangerates.org")
        prov_layout = QVBoxLayout(provider_box)

        form = QFormLayout()
        self._key_edit = QLineEdit()
        self._key_edit.setPlaceholderText(
            "Paste your free app_id here (see openexchangerates.org/signup/free)"
        )
        self._key_edit.setEchoMode(QLineEdit.Password)
        existing_key = self._repo.get_setting("oxr_api_key") or ""
        self._key_edit.setText(existing_key)
        form.addRow("API key:", self._key_edit)
        prov_layout.addLayout(form)

        disclaimer = QLabel(
            "Stored inside this .mfl file. Remove before sharing snapshots."
        )
        tokens.themed(disclaimer, "QLabel { color: {muted}; font-size: 11px; }")
        prov_layout.addWidget(disclaimer)

        action_row = QHBoxLayout()
        self._refresh_status = QLabel(
            f"Last refresh: {_fmt_refresh_time(self._repo.get_setting('oxr_last_refresh_at'))}"
        )
        tokens.themed(self._refresh_status, "QLabel { color: {muted_strong}; }")
        action_row.addWidget(self._refresh_status, 1)
        self._refresh_btn = QPushButton("Refresh now")
        self._refresh_btn.clicked.connect(self._on_refresh_now)
        action_row.addWidget(self._refresh_btn)
        # ADR-065: historical backfill (Refresh now only fetches today's
        # rates). Opens a range + granularity picker.
        self._backfill_btn = QPushButton("Backfill historical…")
        self._backfill_btn.clicked.connect(self._on_backfill_historical)
        action_row.addWidget(self._backfill_btn)
        prov_layout.addLayout(action_row)

        outer.addWidget(provider_box)

        # ── Latest rates table ────────────────────────────────────────────
        rates_box = QGroupBox("Latest known rates")
        rates_layout = QVBoxLayout(rates_box)
        self._rates_table = QTableWidget(0, 4)
        self._rates_table.setHorizontalHeaderLabels(
            ["Base", "Quote", "Rate", "As of"]
        )
        self._rates_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._rates_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._rates_table.verticalHeader().setVisible(False)
        header = self._rates_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        rates_layout.addWidget(self._rates_table)

        # Add-manual-rate row.
        add_row = QHBoxLayout()
        self._add_base = QComboBox()
        self._add_quote = QComboBox()
        # Pre-populate from distinct account currencies + a base set.
        seeded = sorted(set(
            ["USD", "GBP", "EUR", "JPY", "CAD", "AUD", "CHF"]
            + list(self._repo.list_distinct_currencies())
        ))
        for ccy in seeded:
            self._add_base.addItem(ccy)
            self._add_quote.addItem(ccy)
        self._add_base.setCurrentText("USD")
        if len(seeded) > 1 and seeded[0] == "USD":
            self._add_quote.setCurrentText(seeded[1])
        self._add_rate = QLineEdit()
        self._add_rate.setPlaceholderText("e.g. 0.7853")
        self._add_rate.setMaximumWidth(120)
        self._add_date = make_date_edit()
        self._add_btn = QPushButton("Add rate")
        self._add_btn.clicked.connect(self._on_add_manual_rate)

        add_row.addWidget(QLabel("Add manual:"))
        add_row.addWidget(self._add_base)
        add_row.addWidget(QLabel("→"))
        add_row.addWidget(self._add_quote)
        add_row.addWidget(self._add_rate)
        add_row.addWidget(self._add_date)
        add_row.addWidget(self._add_btn)
        rates_layout.addLayout(add_row)

        outer.addWidget(rates_box, 1)

        # ── Matcher tunables (ADR-036) ────────────────────────────────────
        match_box = QGroupBox("Transfer matching")
        match_form = QFormLayout(match_box)
        self._window_spin = QSpinBox()
        self._window_spin.setRange(0, 30)
        self._window_spin.setSuffix(" days")
        win = self._repo.get_setting("transfer_match_window_days") or "3"
        try:
            self._window_spin.setValue(int(win))
        except ValueError:
            self._window_spin.setValue(3)
        match_form.addRow("Match window:", self._window_spin)

        self._fx_tol_spin = QDoubleSpinBox()
        self._fx_tol_spin.setRange(0.0, 25.0)
        self._fx_tol_spin.setSingleStep(0.1)
        self._fx_tol_spin.setSuffix(" %")
        self._fx_tol_spin.setDecimals(1)
        tol = self._repo.get_setting("transfer_fx_tolerance_pct") or "1.0"
        try:
            self._fx_tol_spin.setValue(float(tol))
        except ValueError:
            self._fx_tol_spin.setValue(1.0)
        match_form.addRow("FX rate tolerance:", self._fx_tol_spin)

        match_hint = QLabel(
            "Matcher considers transactions in the destination account "
            "within ± the window; for cross-currency pairs the implied "
            "rate must be within the tolerance of the FX rate for that day."
        )
        match_hint.setWordWrap(True)
        tokens.themed(match_hint, "QLabel { color: {muted}; font-size: 11px; }")
        match_form.addRow(match_hint)

        outer.addWidget(match_box)

        # ── Buttons ───────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Close
        )
        buttons.button(QDialogButtonBox.Save).setDefault(True)
        buttons.accepted.connect(self._on_save_and_close)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._reload_rates_table()

    # ── data refreshers ─────────────────────────────────────────────────

    def _reload_rates_table(self) -> None:
        """Rebuild the rates table from the current ``fx_rate`` contents.

        For each ``(base, quote)`` pair we've ever stored a rate for, we
        show the most recent date + rate. Keeps the user oriented without
        listing every historical row."""
        pairs = self._repo.list_known_rate_pairs()
        # Group: for each pair, query the latest row.
        rows: list[tuple[str, str, str, str]] = []
        for base, quote in pairs:
            r = self._repo.connection.execute(
                "SELECT date, rate FROM fx_rate "
                "WHERE base = ? AND quote = ? "
                "ORDER BY date DESC LIMIT 1",
                (base, quote),
            ).fetchone()
            if r is None:
                continue
            rows.append((base, quote, f"{r['rate']:.6f}", r["date"]))
        self._rates_table.setRowCount(len(rows))
        for i, (b, q, rate, asof) in enumerate(rows):
            self._rates_table.setItem(i, 0, QTableWidgetItem(b))
            self._rates_table.setItem(i, 1, QTableWidgetItem(q))
            rate_item = QTableWidgetItem(rate)
            rate_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._rates_table.setItem(i, 2, rate_item)
            self._rates_table.setItem(i, 3, QTableWidgetItem(asof))

    # ── button handlers ─────────────────────────────────────────────────

    def _on_refresh_now(self) -> None:
        """Synchronous refresh — small wait cursor + a status update.

        The API key field is persisted first so the user can paste and
        click Refresh in one go without having to Save first."""
        typed_key = self._key_edit.text().strip()
        self._repo.set_setting("oxr_api_key", typed_key)
        if not typed_key:
            QMessageBox.warning(
                self, "Need an API key",
                "Add your openexchangerates.org app_id to the field "
                "above before refreshing.",
            )
            return
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
        try:
            result = refresh_latest_into(self._repo, force=True)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "Refresh failed",
                f"Could not refresh rates:\n\n{e}",
            )
            return
        QApplication.restoreOverrideCursor()
        self._refresh_status.setText(
            f"Last refresh: {_fmt_refresh_time(result.fetched_at)} · "
            f"{result.new_rates_count} rate"
            f"{'s' if result.new_rates_count != 1 else ''} updated"
        )
        if result.errors:
            QMessageBox.warning(
                self, "Some rates failed",
                "Refresh finished with errors:\n\n"
                + "\n".join(result.errors),
            )
        self._reload_rates_table()

    def _on_backfill_historical(self) -> None:
        """Open the historical-backfill dialog (ADR-065). Persists the typed
        API key first (same as Refresh) so the user can paste + backfill in
        one go; reloads the rates table if anything was fetched."""
        from mfl_desktop.ui.fx_backfill_dialog import FxBackfillDialog

        typed_key = self._key_edit.text().strip()
        self._repo.set_setting("oxr_api_key", typed_key)
        if not typed_key:
            QMessageBox.warning(
                self, "Need an API key",
                "Add your openexchangerates.org app_id to the field "
                "above before backfilling.",
            )
            return
        dialog = FxBackfillDialog(self._repo, parent=self)
        dialog.exec()
        if dialog.ran_backfill():
            self._refresh_status.setText(
                f"Last refresh: "
                f"{_fmt_refresh_time(self._repo.get_setting('oxr_last_refresh_at'))}"
            )
            self._reload_rates_table()

    def _on_add_manual_rate(self) -> None:
        base = self._add_base.currentText().strip().upper()
        quote = self._add_quote.currentText().strip().upper()
        text = self._add_rate.text().strip()
        on_date = self._add_date.date().toString("yyyy-MM-dd")
        if not text:
            QMessageBox.warning(self, "Add manual rate", "Enter a rate value.")
            return
        try:
            rate = Decimal(text)
        except InvalidOperation:
            QMessageBox.warning(
                self, "Add manual rate",
                f"{text!r} doesn't look like a number.",
            )
            return
        if rate <= 0:
            QMessageBox.warning(
                self, "Add manual rate", "Rate must be greater than zero.",
            )
            return
        if base == quote:
            QMessageBox.warning(
                self, "Add manual rate",
                "Base and quote currencies must differ.",
            )
            return
        try:
            self._repo.upsert_fx_rate(
                date=on_date, base=base, quote=quote,
                rate=rate, source="manual",
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Add manual rate",
                f"Could not save rate:\n\n{e}",
            )
            return
        self._add_rate.clear()
        self._reload_rates_table()

    def _on_save_and_close(self) -> None:
        # Persist any edits to the API key + the matcher tunables.
        self._repo.set_setting("oxr_api_key", self._key_edit.text().strip())
        self._repo.set_setting(
            "transfer_match_window_days", str(self._window_spin.value()),
        )
        self._repo.set_setting(
            "transfer_fx_tolerance_pct",
            f"{self._fx_tol_spin.value():.1f}",
        )
        self.accept()
