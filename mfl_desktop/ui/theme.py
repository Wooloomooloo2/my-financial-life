"""App-wide visual style (ADR-026).

Switches the Qt application off the native Windows platform style and onto
Fusion with a custom QPalette + a small QSS layer. Centralised here so any
future entry point (CLI-launched dialogs, a packaged exe, tests) can apply
the same look with a single call.

Design choices:
- **Fusion** is the cross-platform style. Same widget metrics on Windows,
  macOS, and Linux — and the native `windows11` style reads dated. Fusion
  responds well to QPalette overrides; the native styles often ignore them.
- **Light neutral palette** with a single blue accent. Hex values come from
  the Tailwind v3 slate/blue ramps so future hand-rolled QSS in feature
  windows can pull from the same vocabulary without a colour reference.
- **Minimal QSS layer** rounds inputs/buttons, tidies the table header,
  and brings the menubar/tooltip in line with the palette. Per-widget
  setStyleSheet calls elsewhere in the app still win where they conflict
  — by design; this is a baseline, not a takeover.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# Palette anchors — slate for neutrals, blue-600 for accent.
COLOR_BG_WINDOW        = "#f6f7f9"  # slate-50-ish
COLOR_BG_BASE          = "#ffffff"  # input/table background
COLOR_BG_ALT           = "#f3f4f6"  # alternating row, hover
COLOR_BG_MUTED         = "#e5e7eb"  # borders, dividers
COLOR_TEXT             = "#111827"  # near-black, slate-900
COLOR_TEXT_DIM         = "#6b7280"  # slate-500 (secondary labels)
COLOR_TEXT_DISABLED    = "#9ca3af"  # slate-400
COLOR_ACCENT           = "#2563eb"  # blue-600
COLOR_ACCENT_HOVER     = "#1d4ed8"  # blue-700
COLOR_SELECTION_BG     = "#dbeafe"  # blue-100 — for selections on light surfaces


def apply_theme(app: QApplication) -> None:
    """Apply Fusion + the MFL palette + baseline QSS to ``app``.

    Call once, right after constructing the QApplication, before any window
    is shown.
    """
    app.setStyle("Fusion")
    app.setPalette(_build_palette())
    app.setStyleSheet(_BASE_QSS)


def _build_palette() -> QPalette:
    p = QPalette()

    p.setColor(QPalette.Window,          QColor(COLOR_BG_WINDOW))
    p.setColor(QPalette.WindowText,      QColor(COLOR_TEXT))
    p.setColor(QPalette.Base,            QColor(COLOR_BG_BASE))
    p.setColor(QPalette.AlternateBase,   QColor(COLOR_BG_ALT))
    p.setColor(QPalette.Text,            QColor(COLOR_TEXT))
    p.setColor(QPalette.Button,          QColor(COLOR_BG_BASE))
    p.setColor(QPalette.ButtonText,      QColor(COLOR_TEXT))
    p.setColor(QPalette.BrightText,      QColor("#ffffff"))

    p.setColor(QPalette.Highlight,       QColor(COLOR_ACCENT))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.Link,            QColor(COLOR_ACCENT))
    p.setColor(QPalette.LinkVisited,     QColor(COLOR_ACCENT_HOVER))

    p.setColor(QPalette.ToolTipBase,     QColor(COLOR_TEXT))
    p.setColor(QPalette.ToolTipText,     QColor("#ffffff"))

    # Disabled state — same hue, lower contrast.
    p.setColor(QPalette.Disabled, QPalette.Text,       QColor(COLOR_TEXT_DISABLED))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(COLOR_TEXT_DISABLED))
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor(COLOR_TEXT_DISABLED))
    p.setColor(QPalette.Disabled, QPalette.Highlight,  QColor(COLOR_BG_MUTED))

    return p


# Baseline QSS — kept short and targeted. Per-feature windows can layer
# their own setStyleSheet on top.
_BASE_QSS = f"""
* {{
    font-family: "Segoe UI", "Inter", -apple-system, "Helvetica Neue", Arial, sans-serif;
    font-size: 10pt;
}}

QToolTip {{
    color: #ffffff;
    background-color: {COLOR_TEXT};
    border: 1px solid {COLOR_TEXT};
    padding: 4px 6px;
}}

QMenuBar {{
    background: {COLOR_BG_WINDOW};
    color: {COLOR_TEXT};
    padding: 2px 4px;
}}
QMenuBar::item {{
    padding: 4px 10px;
    background: transparent;
    border-radius: 4px;
}}
QMenuBar::item:selected {{
    background: {COLOR_BG_MUTED};
}}
QMenu {{
    background: {COLOR_BG_BASE};
    border: 1px solid {COLOR_BG_MUTED};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 18px 6px 18px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background: {COLOR_SELECTION_BG};
    color: {COLOR_TEXT};
}}
QMenu::separator {{
    height: 1px;
    background: {COLOR_BG_MUTED};
    margin: 4px 8px;
}}

QStatusBar {{
    background: {COLOR_BG_WINDOW};
    color: {COLOR_TEXT_DIM};
}}
QStatusBar::item {{ border: none; }}

QHeaderView::section {{
    background: {COLOR_BG_ALT};
    color: #374151;
    padding: 6px 8px;
    border: none;
    border-right: 1px solid {COLOR_BG_MUTED};
    border-bottom: 1px solid {COLOR_BG_MUTED};
    font-weight: 600;
}}

QTableView {{
    gridline-color: {COLOR_BG_MUTED};
    selection-background-color: {COLOR_SELECTION_BG};
    selection-color: {COLOR_TEXT};
    alternate-background-color: {COLOR_BG_ALT};
}}
QTableView::item {{
    padding: 2px 4px;
}}

QPushButton {{
    padding: 6px 14px;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    background: {COLOR_BG_BASE};
    color: {COLOR_TEXT};
}}
QPushButton:hover {{
    background: {COLOR_BG_ALT};
}}
QPushButton:pressed {{
    background: {COLOR_BG_MUTED};
}}
QPushButton:default {{
    background: {COLOR_ACCENT};
    color: #ffffff;
    border-color: {COLOR_ACCENT};
}}
QPushButton:default:hover {{
    background: {COLOR_ACCENT_HOVER};
    border-color: {COLOR_ACCENT_HOVER};
}}
QPushButton:disabled {{
    color: {COLOR_TEXT_DISABLED};
    background: {COLOR_BG_ALT};
    border-color: {COLOR_BG_MUTED};
}}

QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox {{
    padding: 4px 8px;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    background: {COLOR_BG_BASE};
    color: {COLOR_TEXT};
    selection-background-color: {COLOR_SELECTION_BG};
    selection-color: {COLOR_TEXT};
}}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {COLOR_ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled {{
    background: {COLOR_BG_ALT};
    color: {COLOR_TEXT_DISABLED};
}}

QComboBox::drop-down {{
    border: none;
    width: 18px;
}}

QListWidget, QTreeWidget, QTreeView {{
    background: {COLOR_BG_BASE};
    border: 1px solid {COLOR_BG_MUTED};
    border-radius: 4px;
}}
QListWidget::item:selected, QTreeWidget::item:selected, QTreeView::item:selected {{
    background: {COLOR_SELECTION_BG};
    color: {COLOR_TEXT};
}}

QSplitter::handle {{
    background: {COLOR_BG_MUTED};
}}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical   {{ height: 1px; }}

QScrollBar:vertical {{
    background: {COLOR_BG_WINDOW};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #cbd5e1;
    border-radius: 6px;
    min-height: 24px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{
    background: #94a3b8;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: {COLOR_BG_WINDOW};
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: #cbd5e1;
    border-radius: 6px;
    min-width: 24px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #94a3b8;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
"""
