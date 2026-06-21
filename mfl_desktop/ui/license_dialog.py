"""Enter-license dialog (ADR-079).

A small modal where the user pastes the license key they were emailed on
purchase. Validates on OK via :func:`license_service.apply_license_key` —
verifying the Ed25519 signature and the edition entitlement entirely
on-device, no network — and shows the error inline (without closing) if the
key is malformed, forged, or for the wrong version. On success it persists the
key and reports the buyer back to the caller so the About box can refresh.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop import license_service
from mfl_desktop.licensing import LicenseError, LicenseInfo
from mfl_desktop.ui import tokens


class LicenseDialog(QDialog):
    """Modal "Enter license key" dialog. After Accepted, :attr:`installed`
    holds the verified :class:`LicenseInfo`."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Enter license key")
        self.setModal(True)
        self.installed: Optional[LicenseInfo] = None

        intro = QLabel(
            "Paste the license key from your purchase confirmation email. "
            "It's checked on this device — no internet connection needed."
        )
        intro.setWordWrap(True)
        tokens.themed(intro, "color: {muted_strong};")

        self._entry = QPlainTextEdit()
        self._entry.setPlaceholderText("Paste your license key here…")
        self._entry.setTabChangesFocus(True)
        self._entry.setFixedHeight(90)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        tokens.themed(self._error, "color: {negative_strong};")

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self,
        )
        buttons.button(QDialogButtonBox.Ok).setText("Activate")
        buy = buttons.addButton("Buy a license…", QDialogButtonBox.ActionRole)
        buy.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(license_service.BUY_URL))
        )
        buttons.accepted.connect(self._on_activate)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)
        layout.addWidget(intro)
        layout.addWidget(self._entry)
        layout.addWidget(self._error)
        layout.addWidget(buttons)
        self.resize(440, self.sizeHint().height())

    def _on_activate(self) -> None:
        text = self._entry.toPlainText().strip()
        try:
            self.installed = license_service.apply_license_key(text)
        except LicenseError as exc:
            self._error.setText(str(exc))
            self._error.setVisible(True)
            return
        self.accept()
