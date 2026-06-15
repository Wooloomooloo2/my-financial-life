"""Shared helpers for the paintEvent charts (ADR-026).

After the chart-engine comparison the owner picked the hand-rolled
paintEvent variant. The Spending Over Time chart and the budget
burn-down chart share these bits so they look consistent:

- ``GROUP_PALETTE`` + ``colour_for`` — the stable, accessible 12-colour
  series. Stack segments and chart series both index into this so the
  same palette runs through every report.
- ``nice_ticks`` — round-number Y-axis ticks (the d3 1/2/5 heuristic).
- ``fmt_currency`` — locale-free GBP formatter (owner is UK-only).
- ``legend_chip`` — swatch + label widget, used by chart legend strips.

Per ADR-026, hex strings here are the same Tailwind v3 vocabulary as the
app palette in ``mfl_desktop/ui/theme.py``.
"""
from __future__ import annotations
from mfl_desktop.ui import tokens

import math

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

# Stable stack colours — indexed by position in the caller's group list.
# Tested at AA contrast against white at 9pt typography.
GROUP_PALETTE: list[str] = [
    "#2563eb",  # blue-600
    "#10b981",  # emerald-500
    "#f59e0b",  # amber-500
    "#ef4444",  # red-500
    "#8b5cf6",  # violet-500
    "#06b6d4",  # cyan-500
    "#ec4899",  # pink-500
    "#84cc16",  # lime-500
    "#f97316",  # orange-500
    "#14b8a6",  # teal-500
    "#a855f7",  # purple-500
    "#64748b",  # slate-500
]


def colour_for(index: int) -> QColor:
    """Stable colour at a given series index — wraps the palette."""
    return QColor(GROUP_PALETTE[index % len(GROUP_PALETTE)])


# ── theme-aware structural chart colours (ADR-076 round 2) ──
# The GROUP_PALETTE series colours read on both themes; these *structural*
# colours (plot background, gridlines, axis text, …) follow the active
# light/dark theme via the design tokens, read fresh at paint time.

def chart_surface() -> str:
    """Plot background, and the thin separators between stacked segments."""
    return tokens.c("surface")


def chart_grid() -> str:
    """Gridlines / faint rules."""
    return tokens.c("border")


def chart_axis_ink() -> str:
    """Axis tick labels and secondary axis text."""
    return tokens.c("muted")


def chart_ink() -> str:
    """Primary on-chart text — value labels, in-chart headings."""
    return tokens.c("text")


def chart_faint() -> str:
    """Faint text — the empty-state 'no data' note, de-emphasised labels."""
    return tokens.c("subtle")


def chart_tooltip_bg() -> str:
    """Hover-tooltip background (near-black in light, near-white in dark)."""
    return tokens.c("text")


def chart_tooltip_ink() -> str:
    """Hover-tooltip text — pairs with chart_tooltip_bg."""
    return tokens.c("surface")


def nice_ticks(vmax: float, target_count: int = 5) -> tuple[float, float]:
    """Return (axis_max, step) so the axis lands on round numbers.

    Mirrors d3's ``ticks`` heuristic: pick a step from {1, 2, 5} × 10ⁿ
    that yields roughly ``target_count`` ticks, then round the axis up
    to the next step boundary.
    """
    if vmax <= 0:
        return 100.0, 20.0
    raw_step = vmax / max(target_count, 1)
    mag = 10 ** math.floor(math.log10(raw_step))
    for m in (1, 2, 5, 10):
        step = m * mag
        if step >= raw_step:
            break
    axis_max = math.ceil(vmax / step) * step
    return axis_max, step


def fmt_currency(pounds: float, decimals: int = 0, symbol: str = "£") -> str:
    """``£1,234`` / ``£1,234.56`` — locale-free. ``symbol`` overrides the
    currency glyph for reports that convert to a chosen display currency."""
    return f"{symbol}{pounds:,.{decimals}f}"


def legend_chip(name: str, colour: QColor) -> QWidget:
    """Swatch + label, side-by-side. Used by chart legend strips."""
    chip = QWidget()
    h = QHBoxLayout(chip)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(6)
    swatch = QLabel()
    swatch.setFixedSize(10, 10)
    swatch.setStyleSheet(
        f"background-color: {colour.name()}; border-radius: 2px;"
    )
    label = QLabel(name)
    tokens.themed(label, "color: {heading}; font-size: 9pt;")
    h.addWidget(swatch)
    h.addWidget(label)
    return chip
