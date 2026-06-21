"""About box (ADR-079, backlog P5).

Shows the app name + version, the current license state (Licensed to X /
Trial: N days left / Trial ended), and the actions to buy or enter a license.
It's the canonical surface for license state per ADR-079, so it re-reads the
status each time it's shown and after a key is entered.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop import license_service
from mfl_desktop.licensing import (
    STATE_EXPIRED,
    STATE_INVALID,
    STATE_LICENSED,
    STATE_TRIAL,
    STATE_WRONG_EDITION,
)
from mfl_desktop.ui import tokens
from mfl_desktop.ui.license_dialog import LicenseDialog
from mfl_desktop.version import APP_NAME, __version__, build_revision


class AboutDialog(QDialog):
    """Modal About box. Self-refreshing license state + Buy / Enter-license."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setModal(True)

        title = QLabel(APP_NAME)
        tokens.themed(title, "QLabel { font-size: 20px; font-weight: 700; color: {heading}; }")
        version = QLabel(f"Version {__version__}")
        tokens.themed(version, "color: {muted};")
        # Build metadata (ADR-099) — "source" in a dev checkout, a CI revision
        # in a packaged build. Surfaced here and in Help ▸ Export Diagnostics.
        build = QLabel(f"Build {build_revision()}")
        tokens.themed(build, "color: {subtle}; font-size: 11px;")

        tagline = QLabel(
            "Your whole financial life — accounts, investments and budgets — "
            "private and on your own device."
        )
        tagline.setWordWrap(True)
        tokens.themed(tagline, "color: {muted_strong};")

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        tokens.themed(line, "QFrame { color: {border}; }")

        # License state — filled by _refresh().
        self._state_lbl = QLabel("")
        self._state_lbl.setWordWrap(True)
        self._state_lbl.setTextFormat(Qt.RichText)

        copyright_lbl = QLabel("© 2026 My Financial Life")
        tokens.themed(copyright_lbl, "color: {muted}; font-size: 11px;")

        buttons = QDialogButtonBox(parent=self)
        self._enter_btn = buttons.addButton(
            "Enter license…", QDialogButtonBox.ActionRole
        )
        self._buy_btn = buttons.addButton("Buy…", QDialogButtonBox.ActionRole)
        buttons.addButton(QDialogButtonBox.Close)
        self._enter_btn.clicked.connect(self._on_enter_license)
        self._buy_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(license_service.BUY_URL))
        )
        buttons.rejected.connect(self.reject)
        # The Close button uses the RejectRole, so wire it to accept/close too.
        close_btn = buttons.button(QDialogButtonBox.Close)
        if close_btn is not None:
            close_btn.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(version)
        layout.addWidget(build)
        layout.addSpacing(4)
        layout.addWidget(tagline)
        layout.addWidget(line)
        layout.addWidget(self._state_lbl)
        layout.addSpacing(2)
        layout.addWidget(copyright_lbl)
        layout.addWidget(buttons)
        self.resize(420, self.sizeHint().height())

        self._refresh()

    def _refresh(self) -> None:
        """Re-read and render the current license status."""
        status = license_service.current_status()
        if status.state == STATE_LICENSED and status.info is not None:
            who = status.info.name or status.info.email or "this device"
            body = (
                f"<b style='color:#1a7f37'>Licensed</b> to {who}"
                f"<br><span>Edition {status.info.edition}.x — thank you!</span>"
            )
            self._enter_btn.setText("Replace license…")
            self._buy_btn.setVisible(False)
        elif status.state == STATE_TRIAL:
            n = status.trial_days_left
            body = (
                f"<b>Free trial</b> — {n} day{'s' if n != 1 else ''} remaining."
                f"<br>All features are unlocked during the trial."
            )
            self._buy_btn.setVisible(True)
        elif status.state in (STATE_WRONG_EDITION, STATE_INVALID):
            body = (
                f"<b style='color:#b35900'>Action needed</b><br>{status.message}"
            )
            self._buy_btn.setVisible(True)
        else:  # expired
            body = (
                "<b style='color:#b42318'>Your free trial has ended.</b>"
                "<br>Buy a license to keep using My Financial Life."
            )
            self._buy_btn.setVisible(True)
        self._state_lbl.setText(body)

    def _on_enter_license(self) -> None:
        dlg = LicenseDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self._refresh()
