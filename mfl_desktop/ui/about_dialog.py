"""About box (backlog P5).

App name, version and build revision (ADR-099), plus the publisher attribution.
Since ADR-193 removed licensing there is no state to re-read — the dialog is
static, so it is built once and shown.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from mfl_desktop import resources
from mfl_desktop.ui import tokens
from mfl_desktop.version import APP_NAME, __version__, build_revision


class AboutDialog(QDialog):
    """Modal About box — app identity, version/build, and publisher."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setModal(True)

        # Brand header: app mark (ADR-103/117) beside the name + version + build.
        # brand_mark = transparent-background hexagon, so it reads cleanly on the
        # About box surface in both light and dark themes.
        icon_lbl = QLabel()
        icon_lbl.setPixmap(resources.brand_mark(64))
        icon_lbl.setFixedSize(64, 64)
        icon_lbl.setScaledContents(True)

        title = QLabel(APP_NAME)
        tokens.themed(title, "QLabel { font-size: 20px; font-weight: 700; color: {heading}; }")
        version = QLabel(f"Version {__version__}")
        tokens.themed(version, "color: {muted};")
        # Build metadata (ADR-099) — "source" in a dev checkout, a CI revision
        # in a packaged build. Surfaced here and in Help ▸ Export Diagnostics.
        build = QLabel(f"Build {build_revision()}")
        tokens.themed(build, "color: {subtle}; font-size: 11px;")

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.addWidget(title)
        text_col.addWidget(version)
        text_col.addWidget(build)
        text_col.addStretch(1)
        header = QHBoxLayout()
        header.setSpacing(14)
        header.addWidget(icon_lbl, 0, Qt.AlignTop)
        header.addLayout(text_col, 1)

        tagline = QLabel(
            "Your whole financial life — accounts, investments and budgets — "
            "private and on your own device."
        )
        tagline.setWordWrap(True)
        tokens.themed(tagline, "color: {muted_strong};")

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        # Brand-gold rule (ADR-100) — a contrast-safe gold accent (a divider,
        # not text) tying the About box to the app icon's gold mark.
        tokens.themed(line, "QFrame { color: {brand_gold}; }")

        # Publisher attribution — the Garelochsoft company wordmark + copyright.
        publisher_lbl = QLabel("Published by")
        tokens.themed(publisher_lbl, "color: {muted}; font-size: 11px;")
        company_logo_lbl = QLabel()
        company_logo_lbl.setPixmap(resources.company_logo(36))
        company_name_lbl = QLabel("Garelochsoft")
        tokens.themed(company_name_lbl, "color: {heading}; font-size: 14px; font-weight: 600;")
        company_row = QHBoxLayout()
        company_row.setSpacing(8)
        company_row.addWidget(publisher_lbl, 0, Qt.AlignVCenter)
        company_row.addWidget(company_logo_lbl, 0, Qt.AlignVCenter)
        company_row.addWidget(company_name_lbl, 0, Qt.AlignVCenter)
        company_row.addStretch(1)

        copyright_lbl = QLabel("© 2026 Garelochsoft")
        tokens.themed(copyright_lbl, "color: {muted}; font-size: 11px;")

        buttons = QDialogButtonBox(parent=self)
        buttons.addButton(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        # The Close button uses the RejectRole, so wire it to accept/close too.
        close_btn = buttons.button(QDialogButtonBox.Close)
        if close_btn is not None:
            close_btn.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addSpacing(4)
        layout.addWidget(tagline)
        layout.addWidget(line)
        layout.addSpacing(6)
        layout.addLayout(company_row)
        layout.addWidget(copyright_lbl)
        layout.addWidget(buttons)
        self.resize(420, self.sizeHint().height())
