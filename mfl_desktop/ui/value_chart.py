"""Hand-rolled cost-basis-vs-market-value bar chart for the investment
dashboard (ADR-045, paintEvent per ADR-026 / [[feedback-chart-engine-preference]]).

One vertical bar per security, two-tone (the owner's spec):

    full height = max(cost, value)
    base (0 .. min(cost, value))  → cost-basis tone (blue)
    tip  (min .. max)             → GREEN when value ≥ cost (gain),
                                     RED   when value < cost (loss)
    unpriced (value is None)      → a single cost-basis bar (no tip), so the
                                     chart is useful before prices are entered

Models `spending_chart.py`: the same gridlines / nice-tick axis / hitmap-hover
machinery and `chart_helpers`, but bars are indexed by security rather than
time bucket and carry exactly two segments. Currency-aware (the account may be
USD) — `chart_helpers.fmt_currency` is GBP-hardcoded, so this widget formats
with a passed-in symbol.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QToolTip, QWidget

from mfl_desktop.ui.chart_helpers import nice_ticks
import mfl_desktop.ui.chart_helpers as _ch
from mfl_desktop.ui.ui_fonts import set_pt

_COLOR_BASE = QColor("#93c5fd")   # blue-300 — cost-basis / invested tone
_COLOR_GAIN = QColor("#16a34a")   # green-600 — appreciation tip
_COLOR_LOSS = QColor("#dc2626")   # red-600 — loss tip


@dataclass(frozen=True)
class ValueBar:
    """One security's bar. ``cost`` and ``value`` are plain floats (account
    currency); ``value`` is None when the security has no price."""
    security_id: int
    label: str               # symbol if present, else name
    name: str                # full name (for the tooltip)
    cost: float
    value: Optional[float]


class ValueChart(QWidget):
    """Cost-basis vs market-value bars, one per security. Emits
    ``bar_clicked(security_id)`` on left-click of a bar (reserved for a future
    drill-down)."""

    bar_clicked = Signal(int)

    _MARGIN_TOP = 24
    _MARGIN_RIGHT = 20
    _MARGIN_LEFT = 78
    _AXIS_LABEL_BAND = 26
    _LEGEND_BAND = 30
    _BAR_SLOT_FILL = 0.62

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self._bars: list[ValueBar] = []
        self._symbol = "$"
        self._empty_message: Optional[str] = None
        # (rect, bar_index) per drawn bar (whole bar is one hit target).
        self._hitmap: list[tuple[QRectF, int]] = []

    # ── public interface ──

    def render(self, bars: list[ValueBar], currency_symbol: str = "$") -> None:
        self._bars = bars
        self._symbol = currency_symbol or ""
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._bars = []
        self._empty_message = message
        self.update()

    # ── formatting ──

    def _fmt(self, amount: float, decimals: int = 0) -> str:
        sym = self._symbol
        sign = "-" if amount < 0 else ""
        a = abs(amount)
        return f"{sign}{sym}{a:,.{decimals}f}" if sym else f"{sign}{a:,.{decimals}f}"

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: D401 — Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(_ch.chart_surface()))

        if self._empty_message is not None or not self._bars:
            self._paint_empty(painter)
            painter.end()
            return

        chart, legend = self._compute_rects()
        ymax, ystep = self._compute_y_axis()
        self._paint_gridlines(painter, chart, ymax, ystep)
        self._paint_y_labels(painter, chart, ymax, ystep)
        self._paint_x_labels(painter, chart)
        self._paint_bars(painter, chart, ymax)
        self._paint_axis_baseline(painter, chart)
        self._paint_legend(painter, legend)
        painter.end()

    def _compute_rects(self) -> tuple[QRectF, QRectF]:
        w, h = self.width(), self.height()
        legend_top = h - self._LEGEND_BAND
        chart_bottom = legend_top - self._AXIS_LABEL_BAND
        chart = QRectF(
            self._MARGIN_LEFT, self._MARGIN_TOP,
            max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
            max(1, chart_bottom - self._MARGIN_TOP),
        )
        legend = QRectF(
            self._MARGIN_LEFT, legend_top,
            max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
            self._LEGEND_BAND,
        )
        return chart, legend

    def _bar_height_value(self, bar: ValueBar) -> float:
        return max(bar.cost, bar.value if bar.value is not None else bar.cost)

    def _compute_y_axis(self) -> tuple[float, float]:
        vmax = max((self._bar_height_value(b) for b in self._bars), default=100.0)
        return nice_ticks(vmax * 1.12)

    def _paint_gridlines(self, painter, chart, ymax, step) -> None:
        pen = QPen(QColor(_ch.chart_grid()))
        pen.setWidth(1)
        painter.setPen(pen)
        n = int(round(ymax / step)) if step > 0 else 0
        for i in range(n + 1):
            y = chart.bottom() - (i * step / ymax) * chart.height()
            painter.drawLine(int(chart.left()), int(y), int(chart.right()), int(y))

    def _paint_y_labels(self, painter, chart, ymax, step) -> None:
        font = QFont(painter.font())
        set_pt(font, 9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        n = int(round(ymax / step)) if step > 0 else 0
        for i in range(n + 1):
            v = i * step
            y = chart.bottom() - (v / ymax) * chart.height()
            label = self._fmt(v)
            tw = fm.horizontalAdvance(label)
            painter.drawText(int(chart.left() - tw - 8),
                             int(y + fm.ascent() / 2 - 2), label)

    def _paint_x_labels(self, painter, chart) -> None:
        font = QFont(painter.font())
        set_pt(font, 8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        n = len(self._bars)
        if n == 0:
            return
        slot_w = chart.width() / n
        sample = 1
        if slot_w < 28:
            sample = max(2, int(math.ceil(28 / slot_w)))
        for i, bar in enumerate(self._bars):
            if i % sample != 0 and i != n - 1:
                continue
            x_center = chart.left() + (i + 0.5) * slot_w
            text = bar.label
            tw = fm.horizontalAdvance(text)
            # Truncate over-long labels to the slot.
            if tw > slot_w and len(text) > 3:
                while text and fm.horizontalAdvance(text + "…") > slot_w:
                    text = text[:-1]
                text = (text + "…") if text else bar.label[:3]
                tw = fm.horizontalAdvance(text)
            painter.drawText(int(x_center - tw / 2),
                             int(chart.bottom() + fm.ascent() + 6), text)

    def _paint_bars(self, painter, chart, ymax) -> None:
        self._hitmap.clear()
        n = len(self._bars)
        if n == 0 or ymax <= 0:
            return
        slot_w = chart.width() / n
        bar_w = min(slot_w * self._BAR_SLOT_FILL, 80.0)
        radius = min(5.0, bar_w / 4)
        painter.setPen(Qt.NoPen)

        for i, bar in enumerate(self._bars):
            x_left = chart.left() + (i + 0.5) * slot_w - bar_w / 2

            def y_for(v: float) -> float:
                return chart.bottom() - (v / ymax) * chart.height()

            if bar.value is None:
                base_v, tip_v, tip_colour = bar.cost, 0.0, None
            elif bar.value >= bar.cost:
                base_v, tip_v, tip_colour = bar.cost, bar.value - bar.cost, _COLOR_GAIN
            else:
                base_v, tip_v, tip_colour = bar.value, bar.cost - bar.value, _COLOR_LOSS

            full_v = base_v + tip_v
            full_top = y_for(full_v)
            whole_rect = QRectF(x_left, full_top, bar_w, chart.bottom() - full_top)

            # Base segment (cost-basis tone), rounded top only if there's no tip.
            base_top = y_for(base_v)
            base_rect = QRectF(x_left, base_top, bar_w, chart.bottom() - base_top)
            self._draw_segment(painter, base_rect, _COLOR_BASE,
                               round_top=(tip_colour is None), radius=radius)

            if tip_colour is not None and tip_v > 0:
                tip_rect = QRectF(x_left, full_top, bar_w, base_top - full_top)
                self._draw_segment(painter, tip_rect, tip_colour,
                                   round_top=True, radius=radius)
                # 1px separator between base and tip (plot background colour).
                sep = QPen(QColor(_ch.chart_surface()))
                sep.setWidth(1)
                painter.setPen(sep)
                painter.drawLine(int(base_rect.left()), int(base_rect.top()),
                                 int(base_rect.right()), int(base_rect.top()))
                painter.setPen(Qt.NoPen)

            self._hitmap.append((whole_rect, i))

    def _draw_segment(self, painter, rect, colour, *, round_top, radius) -> None:
        if round_top and rect.height() > radius * 1.4:
            path = QPainterPath()
            path.moveTo(rect.left(), rect.bottom())
            path.lineTo(rect.left(), rect.top() + radius)
            path.quadTo(rect.left(), rect.top(), rect.left() + radius, rect.top())
            path.lineTo(rect.right() - radius, rect.top())
            path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + radius)
            path.lineTo(rect.right(), rect.bottom())
            path.closeSubpath()
            painter.fillPath(path, colour)
        else:
            painter.fillRect(rect, colour)

    def _paint_axis_baseline(self, painter, chart) -> None:
        pen = QPen(QColor(_ch.chart_faint()))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(int(chart.left()), int(chart.bottom()),
                         int(chart.right()), int(chart.bottom()))

    def _paint_legend(self, painter, legend) -> None:
        font = QFont(painter.font())
        set_pt(font, 9)
        painter.setFont(font)
        fm = QFontMetrics(font)
        items = [("Cost basis", _COLOR_BASE), ("Gain", _COLOR_GAIN), ("Loss", _COLOR_LOSS)]
        x = legend.left()
        sw = 10
        y_text = legend.top() + (legend.height() - fm.height()) / 2 + fm.ascent()
        y_sw = legend.top() + (legend.height() - sw) / 2
        for name, colour in items:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(colour))
            painter.drawRoundedRect(QRectF(x, y_sw, sw, sw), 2, 2)
            painter.setPen(QPen(QColor(_ch.chart_ink())))
            painter.drawText(int(x + sw + 6), int(y_text), name)
            x += sw + 6 + fm.horizontalAdvance(name) + 18

    def _paint_empty(self, painter) -> None:
        message = self._empty_message or "No holdings to chart."
        font = QFont(painter.font())
        set_pt(font, 11)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(message)
        painter.drawText(int((self.width() - tw) / 2), int(self.height() / 2), message)

    # ── hover / click ──

    def mouseMoveEvent(self, event) -> None:  # noqa: D401
        pos = event.position() if hasattr(event, "position") else event.posF()
        for rect, idx in self._hitmap:
            if rect.contains(pos):
                bar = self._bars[idx]
                lines = [bar.name or bar.label, f"Cost {self._fmt(bar.cost, 2)}"]
                if bar.value is None:
                    lines.append("No price")
                else:
                    lines.append(f"Value {self._fmt(bar.value, 2)}")
                    gain = bar.value - bar.cost
                    pct = (gain / bar.cost * 100) if bar.cost else 0.0
                    sign = "+" if gain >= 0 else "-"
                    lines.append(f"{'Gain' if gain >= 0 else 'Loss'} "
                                 f"{sign}{self._fmt(abs(gain), 2)} ({sign}{abs(pct):.1f}%)")
                QToolTip.showText(
                    self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
                    "\n".join(lines), self,
                )
                self.setCursor(QCursor(Qt.PointingHandCursor))
                return
        QToolTip.hideText()
        self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: D401
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position() if hasattr(event, "position") else event.posF()
        for rect, idx in self._hitmap:
            if rect.contains(pos):
                self.bar_clicked.emit(self._bars[idx].security_id)
                return
        super().mousePressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D401
        QToolTip.hideText()
        self.unsetCursor()
        super().leaveEvent(event)
