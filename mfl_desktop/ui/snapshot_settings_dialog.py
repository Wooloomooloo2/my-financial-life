"""Snapshot retention settings (ADR-060).

A small modal that edits the four grandfather-father-son retention knobs stored
per file in the ``setting`` table: the capture cadence, and how long the
keep-all / one-per-day / one-per-month tiers run. Mirrors the currencies-dialog
tunable pattern (``QSpinBox`` ⇄ ``Repository`` settings). A live plain-English
summary line restates the current values so the policy is legible without
mentally compiling the numbers.

On Save the policy is persisted and applied immediately — the existing snapshot
set is pruned to the new tiers — and ``policy_saved`` fires so the owning window
can re-arm the capture timer at the new cadence.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from mfl_desktop import snapshots
from mfl_desktop.db.repository import Repository


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" + ("" if n == 1 else "s")


class SnapshotSettingsDialog(QDialog):
    """Edit the per-file snapshot retention policy. See module docstring."""

    policy_saved = Signal()  # emitted after a successful Save (+ immediate prune)

    def __init__(self, repo: Repository, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.setWindowTitle("Snapshot settings")
        self.setModal(True)

        root = QVBoxLayout(self)

        intro = QLabel(
            "Backups thin out as they age, so you keep deep history without the "
            "disk cost. Newest backups are kept in full, then one a day, then "
            "one a month."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        policy = snapshots.load_policy(repo)
        form = QFormLayout()
        self._interval = self._spin(1, 1440, policy.interval_min, " min")
        self._subdaily = self._spin(1, 24 * 14, policy.subdaily_hours, " hours")
        self._daily = self._spin(0, 366, policy.daily_days, " days")
        self._monthly = self._spin(1, 120, policy.monthly_months, " months")
        form.addRow("Take a backup every", self._interval)
        form.addRow("Keep every backup for", self._subdaily)
        form.addRow("Then one a day for", self._daily)
        form.addRow("Then one a month for", self._monthly)
        root.addLayout(form)

        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet("color: #475569;")  # slate-600
        root.addWidget(self._summary)

        for spin in (self._interval, self._subdaily, self._daily, self._monthly):
            spin.valueChanged.connect(self._update_summary)
        self._update_summary()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _spin(self, lo: int, hi: int, value: int, suffix: str) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.setSuffix(suffix)
        return spin

    def _current_policy(self) -> snapshots.RetentionPolicy:
        return snapshots.RetentionPolicy(
            interval_min=self._interval.value(),
            subdaily_hours=self._subdaily.value(),
            daily_days=self._daily.value(),
            monthly_months=self._monthly.value(),
        )

    def _update_summary(self) -> None:
        p = self._current_policy()
        parts = [f"Capture every {_plural(p.interval_min, 'minute')} while open"]
        parts.append(f"keep all for {_plural(p.subdaily_hours, 'hour')}")
        if p.daily_days:
            parts.append(f"one a day for {_plural(p.daily_days, 'day')}")
        parts.append(f"one a month for {_plural(p.monthly_months, 'month')}")
        self._summary.setText(" · ".join(parts) + ".")

    def _on_save(self) -> None:
        policy = self._current_policy()
        snapshots.save_policy(self._repo, policy)
        # Apply the new tiers to the existing set straight away, so the effect is
        # visible the moment the dialog closes (best-effort, like all pruning).
        try:
            snapshots.prune(self._repo.db_path, policy, datetime.now())
        except Exception:  # noqa: BLE001 — pruning must never block saving
            pass
        self.policy_saved.emit()
        self.accept()
