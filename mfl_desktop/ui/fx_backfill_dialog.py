"""Historical FX backfill dialog (ADR-065).

Wires the long-existing ``fx.backfill_historical`` to a UI (it had no
caller — ADR-035 left it as a backlog item). Pick a date range + sampling
granularity (monthly / weekly / daily); a live "≈ N API calls" estimate
makes the openexchangerates free-tier cost (≈ 1,000 requests/month)
visible before anything runs, per ADR-035's guard-rails. One OXR
``/historical`` call is made per sampled date; the nearest-prior lookup
fills the gaps, so monthly is the sensible default.

USD-base only (free-tier constraint): the quotes are the non-USD
currencies in use. Runs synchronously behind a ``QProgressDialog``;
cancelling stops after the current date (rates already fetched are kept).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QVBoxLayout,
)

from mfl_desktop import fx
from mfl_desktop.db.repository import Repository
from mfl_desktop.ui import tokens

_GRANULARITY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Monthly (~12 / year)",  "monthly"),
    ("Weekly (~52 / year)",   "weekly"),
    ("Daily (~365 / year)",   "daily"),
)
# Above this many sampled dates, double-confirm before spending the calls.
_CONFIRM_THRESHOLD = 60


class FxBackfillDialog(QDialog):
    """Modal range + granularity picker that runs ``fx.backfill_historical``."""

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Backfill historical rates")
        self.setModal(True)
        self.resize(420, 240)

        self._repo = repo
        # USD-base only on the free tier, so quotes = non-USD in use.
        self._quotes = [
            c for c in repo.list_distinct_currencies()
            if c and c.strip().upper() != "USD"
        ]
        self._ran = False  # True if a backfill actually fetched anything

        d_from, d_to = self._default_range()
        self._from = QDateEdit(QDate(d_from.year, d_from.month, d_from.day))
        self._from.setCalendarPopup(True)
        self._from.setDisplayFormat("yyyy-MM-dd")
        self._to = QDateEdit(QDate(d_to.year, d_to.month, d_to.day))
        self._to.setCalendarPopup(True)
        self._to.setDisplayFormat("yyyy-MM-dd")
        self._from.dateChanged.connect(self._update_estimate)
        self._to.dateChanged.connect(self._update_estimate)

        self._granularity = QComboBox()
        for label, value in _GRANULARITY_OPTIONS:
            self._granularity.addItem(label, userData=value)
        self._granularity.currentIndexChanged.connect(self._update_estimate)

        self._estimate = QLabel()
        self._estimate.setWordWrap(True)
        tokens.themed(self._estimate, "color: {muted_strong};")

        form = QFormLayout()
        form.addRow("From:", self._from)
        form.addRow("To:", self._to)
        form.addRow("Sample:", self._granularity)

        intro = QLabel(
            "Fetches USD-based rates from openexchangerates for the "
            "currencies in use. One API call per sampled date; the "
            "nearest-prior lookup fills the gaps between samples."
        )
        intro.setWordWrap(True)
        tokens.themed(intro, "color: {muted};")

        self._buttons = QDialogButtonBox()
        self._backfill_btn = self._buttons.addButton(
            "Backfill", QDialogButtonBox.AcceptRole,
        )
        self._buttons.addButton(QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self._on_backfill)
        self._buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)
        root.addWidget(intro)
        root.addLayout(form)
        root.addWidget(self._estimate)
        root.addWidget(self._buttons)

        if not self._quotes:
            self._estimate.setText(
                "Only USD accounts in use — no foreign rates to backfill."
            )
            self._backfill_btn.setEnabled(False)
        else:
            self._update_estimate()

    # ── public ──

    def ran_backfill(self) -> bool:
        """True if the dialog fetched at least one rate (so the caller
        can refresh its rates table)."""
        return self._ran

    # ── internals ──

    def _default_range(self) -> tuple[date, date]:
        """From = the earliest transaction date (so the backfill covers all
        history), falling back to 5 years ago; To = today."""
        today = date.today()
        try:
            row = self._repo.connection.execute(
                "SELECT MIN(posted_date) FROM txn"
            ).fetchone()
            earliest = date.fromisoformat(row[0]) if row and row[0] else None
        except Exception:
            earliest = None
        d_from = earliest or today.replace(year=today.year - 5)
        return d_from, today

    def _current_granularity(self) -> str:
        return self._granularity.currentData() or "monthly"

    def _update_estimate(self, *_a) -> None:
        try:
            samples = fx.sample_backfill_dates(
                self._from.date().toString(Qt.ISODate),
                self._to.date().toString(Qt.ISODate),
                self._current_granularity(),
            )
        except ValueError:
            self._estimate.setText("From must be on or before To.")
            self._backfill_btn.setEnabled(False)
            return
        n = len(samples)
        quotes = ", ".join(self._quotes)
        self._estimate.setText(
            f"≈ {n} API call{'s' if n != 1 else ''} "
            f"(one per sampled date) for {quotes}."
        )
        self._backfill_btn.setEnabled(True)

    def _on_backfill(self) -> None:
        gran = self._current_granularity()
        d_from = self._from.date().toString(Qt.ISODate)
        d_to = self._to.date().toString(Qt.ISODate)
        try:
            samples = fx.sample_backfill_dates(d_from, d_to, gran)
        except ValueError as e:
            QMessageBox.warning(self, "Backfill", str(e))
            return
        total = len(samples)
        if total >= _CONFIRM_THRESHOLD:
            reply = QMessageBox.question(
                self, "Confirm backfill",
                f"This will make up to {total} openexchangerates API calls "
                f"(one per sampled date). The free tier allows about 1,000 "
                f"per month.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        progress = QProgressDialog(
            "Fetching historical rates…", "Cancel", 0, total, self,
        )
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        def on_progress(done: int, tot: int) -> None:
            progress.setMaximum(tot)
            progress.setValue(done)
            QApplication.processEvents()
            if progress.wasCanceled():
                # Not caught by backfill_historical's `except Exception`
                # (BaseException), so it aborts the loop; rates already
                # upserted are kept.
                raise KeyboardInterrupt

        cancelled = False
        try:
            result = fx.backfill_historical(
                self._repo, quotes=self._quotes,
                date_from=d_from, date_to=d_to,
                granularity=gran, on_progress=on_progress,
            )
        except KeyboardInterrupt:
            cancelled = True
            result = None
        except fx.FxFetchError as e:
            progress.close()
            QMessageBox.warning(self, "Backfill failed", str(e))
            return
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Backfill failed", str(e))
            return
        progress.close()

        if result is not None:
            self._ran = result.new_rates_count > 0
        if cancelled:
            QMessageBox.information(
                self, "Backfill cancelled",
                "Stopped early. Rates fetched before cancelling were kept.",
            )
            self.accept()
            return

        msg = f"Added {result.new_rates_count} historical rate(s)."
        if result.errors:
            shown = "\n".join(result.errors[:8])
            more = (
                f"\n…and {len(result.errors) - 8} more."
                if len(result.errors) > 8 else ""
            )
            QMessageBox.warning(
                self, "Backfill finished with errors",
                f"{msg}\n\nSome dates failed:\n\n{shown}{more}",
            )
        else:
            QMessageBox.information(self, "Backfill complete", msg)
        self.accept()
