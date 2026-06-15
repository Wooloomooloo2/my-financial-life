"""App-wide visual style (ADR-026, ADR-076).

Fusion + a QPalette + a global QSS layer, all built from the semantic design
tokens in ``ui/tokens.py`` so the whole app has one light *and* one dark
theme. ``apply_theme(app, theme)`` is called once on launch and again on every
toggle — it updates the token state (which re-formats per-widget templated
styles and signals the charts) and re-applies the palette + global QSS, so the
switch is live.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from mfl_desktop.ui import tokens

SETTING_KEY = "ui_theme"


def apply_theme(app: QApplication, theme: str = "light") -> None:
    """Apply Fusion + the MFL palette + global QSS for ``theme`` ('light' or
    'dark'). Safe to call repeatedly — used for both launch and live toggle."""
    tokens.set_theme(theme)          # state + re-style registered widgets + signal charts
    app.setStyle("Fusion")
    app.setPalette(_build_palette())
    app.setStyleSheet(_qss())


def _build_palette() -> QPalette:
    t = tokens.c
    p = QPalette()
    p.setColor(QPalette.Window,          QColor(t("canvas")))
    p.setColor(QPalette.WindowText,      QColor(t("text")))
    p.setColor(QPalette.Base,            QColor(t("surface")))
    p.setColor(QPalette.AlternateBase,   QColor(t("surface_alt")))
    p.setColor(QPalette.Text,            QColor(t("text")))
    p.setColor(QPalette.Button,          QColor(t("surface")))
    p.setColor(QPalette.ButtonText,      QColor(t("text")))
    p.setColor(QPalette.BrightText,      QColor("#ffffff"))
    p.setColor(QPalette.PlaceholderText, QColor(t("subtle")))

    p.setColor(QPalette.Highlight,       QColor(t("accent")))
    p.setColor(QPalette.HighlightedText, QColor(t("on_accent")))
    p.setColor(QPalette.Link,            QColor(t("accent")))
    p.setColor(QPalette.LinkVisited,     QColor(t("accent_hover")))

    p.setColor(QPalette.ToolTipBase,     QColor(t("text")))
    p.setColor(QPalette.ToolTipText,     QColor(t("surface")))

    p.setColor(QPalette.Disabled, QPalette.Text,       QColor(t("disabled")))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t("disabled")))
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor(t("disabled")))
    p.setColor(QPalette.Disabled, QPalette.Highlight,  QColor(t("border")))
    return p


def _qss() -> str:
    t = tokens.c
    return f"""
* {{
    font-family: -apple-system, "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif;
    font-size: 10pt;
}}

QToolTip {{
    color: {t("surface")};
    background-color: {t("text")};
    border: 1px solid {t("text")};
    padding: 4px 6px;
}}

QMenuBar {{ background: {t("canvas")}; color: {t("text")}; padding: 2px 4px; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; border-radius: 4px; }}
QMenuBar::item:selected {{ background: {t("border")}; }}
QMenu {{ background: {t("surface")}; border: 1px solid {t("border")}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t("accent_subtle")}; color: {t("text")}; }}
QMenu::separator {{ height: 1px; background: {t("border")}; margin: 4px 8px; }}

QStatusBar {{ background: {t("canvas")}; color: {t("muted")}; }}
QStatusBar::item {{ border: none; }}

QHeaderView::section {{
    background: {t("surface_alt")};
    color: {t("heading")};
    padding: 6px 8px;
    border: none;
    border-right: 1px solid {t("border")};
    border-bottom: 1px solid {t("border")};
    font-weight: 600;
}}

QTableView {{
    gridline-color: {t("border")};
    selection-background-color: {t("accent_subtle")};
    selection-color: {t("text")};
    alternate-background-color: {t("surface_alt")};
}}
QTableView::item {{ padding: 2px 4px; }}

QPushButton {{
    padding: 6px 14px;
    border: 1px solid {t("border_strong")};
    border-radius: 6px;
    background: {t("surface")};
    color: {t("text")};
}}
QPushButton:hover {{ background: {t("surface_alt")}; }}
QPushButton:pressed {{ background: {t("border")}; }}
QPushButton:default {{
    background: {t("accent")};
    color: {t("on_accent")};
    border-color: {t("accent")};
}}
QPushButton:default:hover {{ background: {t("accent_hover")}; border-color: {t("accent_hover")}; }}
QPushButton:disabled {{
    color: {t("disabled")};
    background: {t("surface_alt")};
    border-color: {t("border")};
}}

QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox {{
    padding: 4px 8px;
    border: 1px solid {t("border_strong")};
    border-radius: 6px;
    background: {t("surface")};
    color: {t("text")};
    selection-background-color: {t("accent_subtle")};
    selection-color: {t("text")};
}}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {t("accent")};
}}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled {{
    background: {t("surface_alt")};
    color: {t("disabled")};
}}

/* Inline cell editors inside a table view need to fit the row height tightly. */
QAbstractItemView QLineEdit,
QAbstractItemView QComboBox,
QAbstractItemView QDateEdit {{
    padding: 0px 4px;
    border-radius: 0;
    border: 1px solid {t("accent")};
}}

QListWidget, QTreeWidget, QTreeView {{
    background: {t("surface")};
    border: 1px solid {t("border")};
    border-radius: 4px;
    outline: 0;   /* no focus rectangle around the current item */
}}
QListWidget::item:selected, QTreeWidget::item:selected, QTreeView::item:selected {{
    background: {t("accent_subtle")};
    color: {t("text")};
}}

/* Shared dashboard/section card (ADR-075/076) — themed by object name so it
   switches live without per-instance styling. */
QFrame#homeCard {{
    background: {t("surface")};
    border: 1px solid {t("border")};
    border-radius: 10px;
}}

QSplitter::handle {{ background: {t("border")}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical   {{ height: 1px; }}

QScrollBar:vertical {{ background: {t("canvas")}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {t("border_strong")}; border-radius: 6px; min-height: 24px; margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {t("muted")}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {t("canvas")}; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {t("border_strong")}; border-radius: 6px; min-width: 24px; margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {t("muted")}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
"""
