"""Trailing-period income bar chart for the Investment Income view (ADR-108).

A hand-rolled ``paintEvent`` bar chart (the owner's chosen chart style,
ADR-026): one bar per calendar month over the report window, y = income in the
display currency, with a dashed average line. Theme-aware via the design tokens
and ``chart_helpers`` (ADR-076 / 100) — the bar uses the live brand accent.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from mfl_desktop.ui import chart_helpers, ui_fonts


class IncomeBarChart(QWidget):
    """Monthly income bars. Call :meth:`render` with ``[(label, value), …]``
    (value in the display currency) and the currency symbol."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(180)
        self._bars: list[tuple[str, float]] = []
        self._symbol = ""
        self._empty_msg = "No income in this period."

    def render(self, bars: list[tuple[str, float]], symbol: str) -> None:
        self._bars = list(bars)
        self._symbol = symbol or ""
        self._empty_msg = ""
        self.update()

    def show_empty(self, message: str) -> None:
        self._bars = []
        self._empty_msg = message
        self.update()

    def _font(self, pt: float) -> QFont:
        return ui_fonts.set_pt(QFont(self.font()), pt)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(chart_helpers.chart_surface()))

        if not self._bars or all(v <= 0 for _l, v in self._bars):
            p.setPen(QColor(chart_helpers.chart_axis_ink()))
            p.setFont(self._font(11))
            p.drawText(
                self.rect(), Qt.AlignCenter,
                self._empty_msg or "No income in this period.",
            )
            p.end()
            return

        left, right, top, bottom = 66, 16, 14, 36
        plot_w = max(1, w - left - right)
        plot_h = max(1, h - top - bottom)

        axis_max, step = chart_helpers.nice_ticks(max(v for _l, v in self._bars))
        axis_ink = QColor(chart_helpers.chart_axis_ink())
        grid = QColor(chart_helpers.chart_grid())
        ink = QColor(chart_helpers.chart_ink())
        accent = QColor(chart_helpers.chart_accent())
        sym = self._symbol or "£"

        # ── y gridlines + value labels ──
        p.setFont(self._font(9))
        n_ticks = int(round(axis_max / step)) if step else 0
        for i in range(n_ticks + 1):
            val = i * step
            y = top + plot_h - (val / axis_max) * plot_h
            p.setPen(QPen(grid, 1))
            p.drawLine(left, int(y), w - right, int(y))
            p.setPen(axis_ink)
            p.drawText(
                QRectF(0, y - 8, left - 6, 16),
                Qt.AlignRight | Qt.AlignVCenter,
                chart_helpers.fmt_currency(val, 0, sym),
            )

        # ── bars ──
        n = len(self._bars)
        slot = plot_w / n
        bar_w = min(slot * 0.7, 48)
        for i, (label, val) in enumerate(self._bars):
            cx = left + slot * (i + 0.5)
            bh = (val / axis_max) * plot_h if val > 0 else 0.0
            bar_top = top + plot_h - bh
            p.fillRect(QRectF(cx - bar_w / 2, bar_top, bar_w, bh), accent)
            # Value label above each non-zero bar — the month's income amount
            # (income is usually quarterly, so only a few months are labelled).
            if val > 0:
                p.setPen(ink)
                p.setFont(self._font(8))
                ly = max(float(top), bar_top - 15)
                p.drawText(
                    QRectF(cx - slot / 2, ly, slot, 13),
                    Qt.AlignHCenter | Qt.AlignBottom,
                    chart_helpers.fmt_currency(val, 0, sym),
                )
            # Thin the x labels when the window is long so they don't collide.
            if n <= 14 or i % 2 == 0:
                p.setPen(axis_ink)
                p.setFont(self._font(8))
                p.drawText(
                    QRectF(cx - slot / 2, top + plot_h + 4, slot, 16),
                    Qt.AlignHCenter | Qt.AlignTop, label,
                )

        # ── average line ──
        avg = sum(v for _l, v in self._bars) / n
        if avg > 0:
            y = top + plot_h - (avg / axis_max) * plot_h
            p.setPen(QPen(ink, 1, Qt.DashLine))
            p.drawLine(left, int(y), w - right, int(y))
            p.setPen(axis_ink)
            p.setFont(self._font(8))
            p.drawText(
                QRectF(w - right - 130, y - 16, 130, 14),
                Qt.AlignRight | Qt.AlignBottom,
                f"avg {chart_helpers.fmt_currency(avg, 0, sym)}",
            )
        p.end()
