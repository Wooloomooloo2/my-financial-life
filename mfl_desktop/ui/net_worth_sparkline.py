"""Net-worth trend sparkline for the Home hero (ADR-150; paintEvent per ADR-026).

A deliberately minimal chart — one net-worth line, a soft accent area-fill, and
an emphasized endpoint dot. No axes, no legend, no gridlines: the full
``NetWorthHistoryChart`` owns that job on the Net Worth screen; the hero wants a
quiet sense of direction, not a second report. Structural colours are read from
``ui.tokens`` at paint time, so the ADR-076 light/dark toggle repaints it for
free (``theme.apply_theme`` calls ``update()`` on every widget).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from mfl_desktop.ui import tokens


class NetWorthSparkline(QWidget):
    """Renders a ``[(iso_date, value)]`` series as a filled trend line."""

    _PAD_X = 5.0
    _PAD_TOP = 12.0
    _PAD_BOT = 8.0

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._values: list[float] = []
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def render(self, points: Sequence[tuple[str, object]]) -> None:
        """Set the series (``(date, value)`` ascending). Values may be Decimal,
        int or float. Fewer than two points paints nothing."""
        vals: list[float] = []
        for _d, v in points:
            vals.append(float(v) if not isinstance(v, Decimal) else float(v))
        self._values = vals
        self.update()

    def has_data(self) -> bool:
        return len(self._values) >= 2

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        vals = self._values
        if len(vals) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        w, h = float(self.width()), float(self.height())
        n = len(vals)
        vmin, vmax = min(vals), max(vals)
        span = (vmax - vmin) or 1.0
        left, right = self._PAD_X, w - self._PAD_X
        top, bottom = self._PAD_TOP, h - self._PAD_BOT

        def x_at(i: int) -> float:
            return left + (i / (n - 1)) * (right - left)

        def y_at(v: float) -> float:
            return top + (1.0 - (v - vmin) / span) * (bottom - top)

        line = QPainterPath()
        line.moveTo(x_at(0), y_at(vals[0]))
        for i in range(1, n):
            line.lineTo(x_at(i), y_at(vals[i]))

        # Area = the line closed down to the card floor, filled with an accent
        # gradient that fades to nothing.
        area = QPainterPath(line)
        area.lineTo(x_at(n - 1), h)
        area.lineTo(x_at(0), h)
        area.closeSubpath()

        accent = QColor(tokens.c("accent"))
        grad = QLinearGradient(0.0, top, 0.0, h)
        top_c = QColor(accent)
        top_c.setAlpha(70)
        bot_c = QColor(accent)
        bot_c.setAlpha(0)
        grad.setColorAt(0.0, top_c)
        grad.setColorAt(1.0, bot_c)
        painter.fillPath(area, QBrush(grad))

        pen = QPen(accent)
        pen.setWidthF(2.4)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(line)

        # Endpoint marker: a surface-coloured casing so it reads over the fill,
        # then an accent core.
        ex, ey = x_at(n - 1), y_at(vals[-1])
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(tokens.c("surface"))))
        painter.drawEllipse(QPointF(ex, ey), 5.5, 5.5)
        painter.setBrush(QBrush(accent))
        painter.drawEllipse(QPointF(ex, ey), 3.2, 3.2)
        painter.end()
