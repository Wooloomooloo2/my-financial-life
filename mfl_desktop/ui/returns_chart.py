"""Total-return composition chart for the Investment Returns report (ADR-046,
paintEvent per ADR-026 / [[feedback-chart-engine-preference]]).

A stacked-area composition over time, drawn *relative to the cost line* so the
breakdown the owner asked for — cost / returns / dividends — reads at a glance:

- **Cost basis** (blue): your invested capital still intact, ``min(cost, value)``.
- **Appreciation** between ``min(cost, value)`` and ``max(cost, value)``: green when
  the holdings are above cost (unrealized gain), **red** when below (the part of
  cost currently underwater).
- **Realized** (teal): realized gains *within the window*, stacked above the value;
  a net realized loss draws downward in muted red.
- **Dividends** (gold): dividend / income received *within the window*, on top.

Realized and dividends are period-scoped by the engine (they accumulate from zero
at the window's left edge); unrealized is the lifetime gain of currently-held
shares. Consumes ``holdings.ReturnPoint``s; currency-aware (the account may be USD).
Market value falls back to cost where prices are missing — flagged with a note.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QToolTip, QWidget

from mfl_desktop.ui.chart_helpers import nice_ticks
import mfl_desktop.ui.chart_helpers as _ch

_COLOR_COST = QColor("#2563eb")        # blue-600 — invested capital
_COLOR_GAIN = QColor("#16a34a")        # green-600 — unrealized gain
_COLOR_LOSS = QColor("#dc2626")        # red-600 — underwater cost
_COLOR_REALIZED = QColor("#0d9488")    # teal-600 — realized gains
_COLOR_REALIZED_NEG = QColor("#f87171")  # red-400 — realized loss (downward)
_COLOR_DIVIDEND = QColor("#d97706")    # amber-600 — dividends / income
_COLOR_COST_LINE = QColor("#1e3a8a")   # blue-900 — cost reference line


class ReturnsChart(QWidget):
    """Stacked total-return composition over time (ADR-046)."""

    _MARGIN_TOP = 24
    _MARGIN_RIGHT = 20
    _MARGIN_LEFT = 88
    _AXIS_LABEL_BAND = 24
    _LEGEND_BAND = 30

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self._points: list = []          # list[ReturnPoint]
        self._symbol = "$"
        self._any_fallback = False
        self._empty_message: Optional[str] = None
        self._x_positions: list[tuple[float, int]] = []

    # ── public ──

    def render(self, points: list, currency_symbol: str = "$",
               any_fallback: bool = False) -> None:
        self._points = points
        self._symbol = currency_symbol or ""
        self._any_fallback = any_fallback
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._points = []
        self._empty_message = message
        self.update()

    # ── formatting ──

    def _fmt(self, amount: float, decimals: int = 0) -> str:
        sym = self._symbol
        sign = "-" if amount < 0 else ""
        a = abs(amount)
        return f"{sign}{sym}{a:,.{decimals}f}" if sym else f"{sign}{a:,.{decimals}f}"

    @staticmethod
    def _month_label(iso: str) -> str:
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            return iso
        return f"{d.strftime('%b')} {d.strftime('%y')}"

    # ── per-point band boundaries (in currency units) ──

    @staticmethod
    def _bounds(p) -> tuple[float, float, float, float, float, float]:
        """Return the six stacking boundaries for one point, bottom→top:
        ``(0, cost_low, value_top, realized_top, dividends_top, cost)`` where
        ``cost_low = min(cost, mv)``, ``value_top = max(cost, mv)``,
        ``realized_top = value_top + realized``, ``dividends_top =
        realized_top + dividends``. ``cost`` is returned separately for the
        reference line."""
        cost = float(p.cost_basis)
        mv = float(p.market_value)
        realized = float(p.realized_cum)
        div = float(p.dividends_cum)
        cost_low = min(cost, mv)
        value_top = max(cost, mv)
        realized_top = value_top + realized
        dividends_top = realized_top + div
        return 0.0, cost_low, value_top, realized_top, dividends_top, cost

    # ── painting ──

    def paintEvent(self, event) -> None:  # noqa: D401
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(_ch.chart_surface()))

        if self._empty_message is not None or len(self._points) < 2:
            self._paint_empty(
                painter,
                self._empty_message or "Not enough history to chart yet.",
            )
            painter.end()
            return

        chart, legend = self._compute_rects()
        # y-range: top from the tallest stack, bottom clamped at 0 unless a
        # realized loss pushes a stack below zero.
        tops = []
        bottoms = [0.0]
        for p in self._points:
            _, _, value_top, realized_top, dividends_top, _ = self._bounds(p)
            tops.append(max(value_top, dividends_top))
            bottoms.append(min(realized_top, dividends_top, 0.0))
        vmax = max(tops) if tops else 0.0
        vmin = min(bottoms)
        ymax, ystep = nice_ticks(vmax * 1.1 if vmax > 0 else 1.0)
        ymin = 0.0
        if vmin < 0:
            # Extend the axis downward in whole steps to fit a realized loss.
            steps_down = int((-vmin) / ystep) + 1 if ystep > 0 else 0
            ymin = -steps_down * ystep

        self._paint_gridlines(painter, chart, ymin, ymax, ystep)
        self._paint_y_labels(painter, chart, ymin, ymax, ystep)
        self._paint_x_labels(painter, chart)
        self._paint_bands(painter, chart, ymin, ymax)
        self._paint_cost_line(painter, chart, ymin, ymax)
        self._paint_zero_baseline(painter, chart, ymin, ymax)
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
        legend = QRectF(self._MARGIN_LEFT, legend_top,
                        max(1, w - self._MARGIN_LEFT - self._MARGIN_RIGHT),
                        self._LEGEND_BAND)
        return chart, legend

    def _x_for(self, i: int, chart: QRectF) -> float:
        n = len(self._points)
        if n <= 1:
            return chart.left()
        return chart.left() + (i / (n - 1)) * chart.width()

    def _y_for(self, v: float, ymin: float, ymax: float, chart: QRectF) -> float:
        span = ymax - ymin
        if span <= 0:
            return chart.bottom()
        return chart.bottom() - ((v - ymin) / span) * chart.height()

    def _paint_gridlines(self, painter, chart, ymin, ymax, step) -> None:
        pen = QPen(QColor(_ch.chart_grid()))
        pen.setWidth(1)
        painter.setPen(pen)
        if step <= 0:
            return
        v = ymin
        while v <= ymax + 1e-6:
            y = self._y_for(v, ymin, ymax, chart)
            painter.drawLine(int(chart.left()), int(y), int(chart.right()), int(y))
            v += step

    def _paint_y_labels(self, painter, chart, ymin, ymax, step) -> None:
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        if step <= 0:
            return
        v = ymin
        while v <= ymax + 1e-6:
            y = self._y_for(v, ymin, ymax, chart)
            label = self._fmt(v)
            tw = fm.horizontalAdvance(label)
            painter.drawText(int(chart.left() - tw - 8),
                             int(y + fm.ascent() / 2 - 2), label)
            v += step

    def _paint_x_labels(self, painter, chart) -> None:
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        n = len(self._points)
        if n == 0:
            return
        sample = max(1, n // 8)
        for i, p in enumerate(self._points):
            if i % sample != 0 and i != n - 1:
                continue
            x = self._x_for(i, chart)
            text = self._month_label(p.date)
            tw = fm.horizontalAdvance(text)
            painter.drawText(int(x - tw / 2),
                             int(chart.bottom() + fm.ascent() + 6), text)

    def _paint_bands(self, painter, chart, ymin, ymax) -> None:
        """Draw each composition band as per-segment quadrilaterals between
        consecutive samples, so a band can change colour (gain↔loss) mid-run."""
        self._x_positions = []
        n = len(self._points)
        for i, p in enumerate(self._points):
            self._x_positions.append((self._x_for(i, chart), i))

        painter.setPen(Qt.NoPen)
        for i in range(n - 1):
            p0, p1 = self._points[i], self._points[i + 1]
            x0 = self._x_for(i, chart)
            x1 = self._x_for(i + 1, chart)
            b0 = self._bounds(p0)
            b1 = self._bounds(p1)
            _, cl0, vt0, rt0, dt0, _ = b0
            _, cl1, vt1, rt1, dt1, _ = b1

            def quad(lo0, hi0, lo1, hi1):
                return QPolygonF([
                    QPointF(x0, self._y_for(hi0, ymin, ymax, chart)),
                    QPointF(x1, self._y_for(hi1, ymin, ymax, chart)),
                    QPointF(x1, self._y_for(lo1, ymin, ymax, chart)),
                    QPointF(x0, self._y_for(lo0, ymin, ymax, chart)),
                ])

            # 1. Cost (intact capital): 0 → cost_low
            painter.setBrush(QBrush(_COLOR_COST))
            painter.drawPolygon(quad(0.0, cl0, 0.0, cl1))

            # 2. Appreciation: cost_low → value_top, green if gain else red.
            #    Sign decided on the segment average (matches value-history fill).
            gain0 = float(p0.unrealized)
            gain1 = float(p1.unrealized)
            is_gain = (gain0 + gain1) >= 0
            painter.setBrush(QBrush(_COLOR_GAIN if is_gain else _COLOR_LOSS))
            painter.drawPolygon(quad(cl0, vt0, cl1, vt1))

            # 3. Realized: value_top → realized_top (teal up, red down).
            r_up = (rt0 - vt0) + (rt1 - vt1) >= 0
            painter.setBrush(QBrush(_COLOR_REALIZED if r_up else _COLOR_REALIZED_NEG))
            painter.drawPolygon(quad(vt0, rt0, vt1, rt1))

            # 4. Dividends: realized_top → dividends_top
            painter.setBrush(QBrush(_COLOR_DIVIDEND))
            painter.drawPolygon(quad(rt0, dt0, rt1, dt1))

    def _paint_cost_line(self, painter, chart, ymin, ymax) -> None:
        pen = QPen(_COLOR_COST_LINE)
        pen.setWidth(2)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        pts = [
            QPointF(self._x_for(i, chart),
                    self._y_for(float(p.cost_basis), ymin, ymax, chart))
            for i, p in enumerate(self._points)
        ]
        painter.drawPolyline(QPolygonF(pts))

    def _paint_zero_baseline(self, painter, chart, ymin, ymax) -> None:
        pen = QPen(QColor(_ch.chart_faint()))
        pen.setWidth(1)
        painter.setPen(pen)
        y = self._y_for(0.0, ymin, ymax, chart)
        painter.drawLine(int(chart.left()), int(y), int(chart.right()), int(y))

    def _paint_legend(self, painter, legend) -> None:
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        fm = QFontMetrics(font)
        items = [
            ("Cost basis", _COLOR_COST),
            ("Gain / loss", _COLOR_GAIN),
            ("Realized", _COLOR_REALIZED),
            ("Dividends", _COLOR_DIVIDEND),
        ]
        x = legend.left()
        y_text = legend.top() + (legend.height() - fm.height()) / 2 + fm.ascent()
        y_chip = legend.top() + legend.height() / 2
        for name, colour in items:
            painter.setBrush(QBrush(colour))
            painter.setPen(Qt.NoPen)
            painter.drawRect(int(x), int(y_chip - 6), 12, 12)
            painter.setPen(QPen(QColor(_ch.chart_ink())))
            painter.drawText(int(x + 18), int(y_text), name)
            x += 18 + fm.horizontalAdvance(name) + 20
        if self._any_fallback:
            painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
            note = "· early periods use cost where prices are unavailable"
            painter.drawText(int(x), int(y_text), note)

    def _paint_empty(self, painter, message: str) -> None:
        font = QFont(painter.font())
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(message)
        painter.drawText(int((self.width() - tw) / 2), int(self.height() / 2), message)

    # ── hover ──

    def mouseMoveEvent(self, event) -> None:  # noqa: D401
        if not self._x_positions:
            super().mouseMoveEvent(event)
            return
        pos = event.position() if hasattr(event, "position") else event.posF()
        nearest = min(self._x_positions, key=lambda xp: abs(xp[0] - pos.x()))
        idx = nearest[1]
        p = self._points[idx]
        cost = float(p.cost_basis)
        mv = float(p.market_value)
        unreal = float(p.unrealized)
        realized = float(p.realized_cum)
        div = float(p.dividends_cum)
        total = unreal + realized + div
        u_sign = "+" if unreal >= 0 else "-"
        u_pct = (unreal / cost * 100) if cost else 0.0
        t_sign = "+" if total >= 0 else "-"
        t_pct = (total / cost * 100) if cost else 0.0
        text = (
            f"{self._month_label(p.date)}\n"
            f"Cost basis {self._fmt(cost)}\n"
            f"Market value {self._fmt(mv)}\n"
            f"Unrealized {u_sign}{self._fmt(abs(unreal))} ({u_sign}{abs(u_pct):.1f}%)\n"
            f"Realized (period) {self._fmt(realized)}\n"
            f"Dividends (period) {self._fmt(div)}\n"
            f"Total return {t_sign}{self._fmt(abs(total))} ({t_sign}{abs(t_pct):.1f}%)"
        )
        QToolTip.showText(
            self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))), text, self,
        )
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D401
        QToolTip.hideText()
        super().leaveEvent(event)
