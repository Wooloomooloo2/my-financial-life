"""Net-worth-over-time chart (ADR-121, paintEvent per ADR-026).

A stacked-area composition over time: asset families stacked **above** the zero
line, debt families stacked **below** it, and a bold **net-worth** polyline on
top — so both the composition and the bottom line read at a glance. Family
colours are passed in from the Net Worth screen's ``_FAMILY_VIEW`` so they match
the point-in-time donut. Consumes ``net_worth_history.NetWorthPoint``s; the
values are already FX-converted to the display currency (ADR-055).

Honours the no-pies rule (ADR-018) — it's an area chart, not a pie.
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
from mfl_desktop.ui.ui_fonts import set_pt


class NetWorthHistoryChart(QWidget):
    """Stacked assets-up / debts-down area + net-worth line over time."""

    _MARGIN_TOP = 24
    _MARGIN_RIGHT = 20
    _MARGIN_LEFT = 92
    _AXIS_LABEL_BAND = 24
    _LEGEND_BAND = 30

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self._points: list = []          # list[NetWorthPoint]
        # Ordered (key, label, QColor) bottom→top for each stack.
        self._asset_families: list[tuple[str, str, QColor]] = []
        self._debt_families: list[tuple[str, str, QColor]] = []
        self._symbol = "£"
        self._any_excluded = False
        self._empty_message: Optional[str] = None
        self._x_positions: list[tuple[float, int]] = []

    # ── public ──

    def render(
        self,
        *,
        points: list,
        asset_families: list[tuple[str, str, QColor]],
        debt_families: list[tuple[str, str, QColor]],
        symbol: str = "£",
        any_excluded: bool = False,
    ) -> None:
        self._points = points
        self._asset_families = asset_families
        self._debt_families = debt_families
        self._symbol = symbol or ""
        self._any_excluded = any_excluded
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

    # ── per-point stack boundaries (in display-currency units) ──

    def _asset_bounds(self, p) -> list[float]:
        """Cumulative asset boundaries bottom→top: ``[0, f1, f1+f2, …]`` over
        the asset families in display order."""
        out = [0.0]
        run = 0.0
        for key, _label, _color in self._asset_families:
            run += float(p.family_assets.get(key, 0.0))
            out.append(run)
        return out

    def _debt_bounds(self, p) -> list[float]:
        """Cumulative debt boundaries going downward: ``[0, -d1, -(d1+d2), …]``."""
        out = [0.0]
        run = 0.0
        for key, _label, _color in self._debt_families:
            run += float(p.family_debts.get(key, 0.0))
            out.append(-run)
        return out

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

        tops = []
        bottoms = [0.0]
        for p in self._points:
            tops.append(max(self._asset_bounds(p)[-1], float(p.net)))
            bottoms.append(min(self._debt_bounds(p)[-1], float(p.net), 0.0))
        vmax = max(tops) if tops else 1.0
        vmin = min(bottoms)
        ymax, ystep = nice_ticks(vmax * 1.08 if vmax > 0 else 1.0)
        ymin = 0.0
        if vmin < 0 and ystep > 0:
            steps_down = int((-vmin) / ystep) + 1
            ymin = -steps_down * ystep

        self._paint_gridlines(painter, chart, ymin, ymax, ystep)
        self._paint_y_labels(painter, chart, ymin, ymax, ystep)
        self._paint_x_labels(painter, chart)
        self._paint_bands(painter, chart, ymin, ymax)
        self._paint_zero_baseline(painter, chart, ymin, ymax)
        self._paint_net_line(painter, chart, ymin, ymax)
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
        set_pt(font, 9)
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

    def _x_label_layout(
        self, chart: QRectF, fm: QFontMetrics,
    ) -> list[tuple[float, str, float]]:
        """Pick x-axis tick labels and pixel spans, dropping overlaps; the final
        point (end date) always wins a collision (mirrors ReturnsChart)."""
        n = len(self._points)
        if n == 0:
            return []
        sample = max(1, n // 8)
        idxs = [i for i in range(n) if i % sample == 0]
        if idxs[-1] != n - 1:
            idxs.append(n - 1)

        spans: list[tuple[float, str, float]] = []
        for i in idxs:
            x = self._x_for(i, chart)
            text = self._month_label(self._points[i].date)
            spans.append((x, text, fm.horizontalAdvance(text) / 2.0))

        gap = 6.0
        kept: list[tuple[float, str, float]] = []
        for j, (x, text, hw) in enumerate(spans):
            is_last = j == len(spans) - 1
            if kept:
                prev_x, _, prev_hw = kept[-1]
                if x - hw < prev_x + prev_hw + gap:
                    if is_last:
                        kept.pop()
                    else:
                        continue
            kept.append((x, text, hw))
        return kept

    def _paint_x_labels(self, painter, chart) -> None:
        font = QFont(painter.font())
        set_pt(font, 8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
        fm = QFontMetrics(font)
        y = int(chart.bottom() + fm.ascent() + 6)
        for x, text, hw in self._x_label_layout(chart, fm):
            painter.drawText(int(x - hw), y, text)

    def _paint_bands(self, painter, chart, ymin, ymax) -> None:
        """Draw each family band as per-segment quads between consecutive
        samples — assets above zero, debts below."""
        self._x_positions = [(self._x_for(i, chart), i) for i in range(len(self._points))]
        painter.setPen(Qt.NoPen)
        n = len(self._points)

        def draw_stack(families, bounds_fn):
            for band_idx, (_key, _label, color) in enumerate(families):
                painter.setBrush(QBrush(color))
                for i in range(n - 1):
                    b0 = bounds_fn(self._points[i])
                    b1 = bounds_fn(self._points[i + 1])
                    x0 = self._x_for(i, chart)
                    x1 = self._x_for(i + 1, chart)
                    poly = QPolygonF([
                        QPointF(x0, self._y_for(b0[band_idx + 1], ymin, ymax, chart)),
                        QPointF(x1, self._y_for(b1[band_idx + 1], ymin, ymax, chart)),
                        QPointF(x1, self._y_for(b1[band_idx], ymin, ymax, chart)),
                        QPointF(x0, self._y_for(b0[band_idx], ymin, ymax, chart)),
                    ])
                    painter.drawPolygon(poly)

        draw_stack(self._asset_families, self._asset_bounds)
        draw_stack(self._debt_families, self._debt_bounds)

    def _paint_zero_baseline(self, painter, chart, ymin, ymax) -> None:
        pen = QPen(QColor(_ch.chart_axis_ink()))
        pen.setWidth(1)
        painter.setPen(pen)
        y = self._y_for(0.0, ymin, ymax, chart)
        painter.drawLine(int(chart.left()), int(y), int(chart.right()), int(y))

    def _paint_net_line(self, painter, chart, ymin, ymax) -> None:
        pts = [
            QPointF(self._x_for(i, chart),
                    self._y_for(float(p.net), ymin, ymax, chart))
            for i, p in enumerate(self._points)
        ]
        # White casing under a dark line so it stays legible over any band.
        casing = QPen(QColor(_ch.chart_surface()))
        casing.setWidth(5)
        casing.setJoinStyle(Qt.RoundJoin)
        casing.setCapStyle(Qt.RoundCap)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(casing)
        painter.drawPolyline(QPolygonF(pts))
        line = QPen(QColor(_ch.chart_ink()))
        line.setWidth(3)
        line.setJoinStyle(Qt.RoundJoin)
        line.setCapStyle(Qt.RoundCap)
        painter.setPen(line)
        painter.drawPolyline(QPolygonF(pts))

    def _paint_legend(self, painter, legend) -> None:
        font = QFont(painter.font())
        set_pt(font, 9)
        painter.setFont(font)
        fm = QFontMetrics(font)
        items: list[tuple[str, QColor, bool]] = []
        for _key, label, color in self._asset_families:
            items.append((label, color, False))
        for _key, label, color in self._debt_families:
            items.append((label, color, False))
        items.append(("Net worth", QColor(_ch.chart_ink()), True))

        x = legend.left()
        y_text = legend.top() + (legend.height() - fm.height()) / 2 + fm.ascent()
        y_chip = legend.top() + legend.height() / 2
        for name, colour, is_line in items:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(colour))
            if is_line:
                painter.drawRect(int(x), int(y_chip - 1), 14, 3)
            else:
                painter.drawRect(int(x), int(y_chip - 6), 12, 12)
            painter.setPen(QPen(QColor(_ch.chart_ink())))
            painter.drawText(int(x + 18), int(y_text), name)
            x += 18 + fm.horizontalAdvance(name) + 18
        if self._any_excluded:
            painter.setPen(QPen(QColor(_ch.chart_axis_ink())))
            painter.drawText(
                int(x), int(y_text),
                "· some accounts excluded where no exchange rate was on file",
            )

    def _paint_empty(self, painter, message: str) -> None:
        font = QFont(painter.font())
        set_pt(font, 11)
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
        p = self._points[nearest[1]]
        text = (
            f"{self._month_label(p.date)}\n"
            f"Net worth {self._fmt(float(p.net))}\n"
            f"Assets {self._fmt(float(p.asset_total))}\n"
            f"Debts {self._fmt(float(p.debt_total))}"
        )
        QToolTip.showText(
            self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))), text, self,
        )
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D401
        QToolTip.hideText()
        super().leaveEvent(event)
