"""Squarified treemap for portfolio allocation (ADR-045, paintEvent per
ADR-026 / [[feedback-chart-engine-preference]]).

Nested rectangles sized by each security's market value (cost-basis fallback
when nothing is priced yet). Scales to ~30 holdings without the sliver problem
a pie has at that count, and the dominant positions read at a glance — which is
why it was chosen over a pie (the ADR-018 no-pie rule still stands).

The squarify layout follows Bruls, Huizing & van Wijk (2000): greedily grow a
row along the shorter edge while it keeps aspect ratios near 1, then lay it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QToolTip, QWidget

from mfl_desktop.ui.chart_helpers import colour_for


@dataclass(frozen=True)
class TreemapTile:
    label: str          # symbol (or short name) shown in the tile
    name: str           # full name for the tooltip
    value: float        # area weight (market value or cost basis)


def _worst_ratio(row: list[float], length: float) -> float:
    total = sum(row)
    if total <= 0 or length <= 0:
        return float("inf")
    rmax, rmin = max(row), min(row)
    return max((length * length * rmax) / (total * total),
               (total * total) / (length * length * rmin))


def _layout_row(row: list[float], x: float, y: float, dx: float, dy: float):
    """Place ``row`` (areas) along the shorter side; return (rects, new free box)."""
    total = sum(row)
    rects = []
    if dx >= dy:
        w = total / dy if dy else 0.0
        cy = y
        for s in row:
            h = (s / w) if w else 0.0
            rects.append(QRectF(x, cy, w, h))
            cy += h
        return rects, x + w, y, dx - w, dy
    else:
        h = total / dx if dx else 0.0
        cx = x
        for s in row:
            w = (s / h) if h else 0.0
            rects.append(QRectF(cx, y, w, h))
            cx += w
        return rects, x, y + h, dx, dy - h


def _squarify(values: list[float], x: float, y: float, dx: float, dy: float) -> list[QRectF]:
    """``values`` already scaled so sum == dx*dy, sorted descending."""
    rects: list[QRectF] = []
    remaining = list(values)
    current: list[float] = []
    while remaining:
        length = min(dx, dy)
        nxt = remaining[0]
        if not current or _worst_ratio(current, length) >= _worst_ratio(current + [nxt], length):
            current.append(remaining.pop(0))
        else:
            placed, x, y, dx, dy = _layout_row(current, x, y, dx, dy)
            rects.extend(placed)
            current = []
    if current:
        placed, x, y, dx, dy = _layout_row(current, x, y, dx, dy)
        rects.extend(placed)
    return rects


class TreemapChart(QWidget):
    """Allocation treemap. ``subtitle`` (e.g. 'by market value') is painted
    top-left; ``footnote`` (e.g. 'N unpriced excluded') bottom-left."""

    _MARGIN = 6

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self._tiles: list[TreemapTile] = []
        self._symbol = "$"
        self._subtitle = ""
        self._footnote = ""
        self._empty_message: Optional[str] = None
        # (rect, tile_index) for hover.
        self._hitmap: list[tuple[QRectF, int]] = []

    def render(self, tiles: list[TreemapTile], currency_symbol: str = "$",
               subtitle: str = "", footnote: str = "") -> None:
        # Largest first — squarify wants descending, and it reads better.
        self._tiles = sorted(
            [t for t in tiles if t.value > 0], key=lambda t: -t.value,
        )
        self._symbol = currency_symbol or ""
        self._subtitle = subtitle
        self._footnote = footnote
        self._empty_message = None
        self.update()

    def show_empty(self, message: str) -> None:
        self._tiles = []
        self._empty_message = message
        self.update()

    def _fmt(self, amount: float) -> str:
        return f"{self._symbol}{amount:,.0f}" if self._symbol else f"{amount:,.0f}"

    def paintEvent(self, event) -> None:  # noqa: D401
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        if self._empty_message is not None or not self._tiles:
            self._paint_empty(painter, self._empty_message or "Nothing to show.")
            painter.end()
            return

        m = self._MARGIN
        top_band = 18 if self._subtitle else 0
        bottom_band = 16 if self._footnote else 0
        x0, y0 = m, m + top_band
        dx = max(1.0, self.width() - 2 * m)
        dy = max(1.0, self.height() - 2 * m - top_band - bottom_band)

        total = sum(t.value for t in self._tiles)
        scaled = [t.value / total * (dx * dy) for t in self._tiles]
        rects = _squarify(scaled, x0, y0, dx, dy)

        self._hitmap = []
        font = QFont(painter.font())
        font.setPointSize(8)
        fm = QFontMetrics(font)
        for i, (tile, rect) in enumerate(zip(self._tiles, rects)):
            colour = colour_for(i)
            painter.setPen(QPen(QColor("#ffffff"), 1))
            painter.setBrush(QBrush(colour))
            painter.drawRect(rect)
            self._hitmap.append((rect, i))
            # Label if the tile is big enough to read.
            if rect.width() > 44 and rect.height() > 22:
                pct = tile.value / total * 100
                painter.setFont(font)
                painter.setPen(QPen(QColor("#ffffff")))
                line1 = tile.label
                if fm.horizontalAdvance(line1) <= rect.width() - 8:
                    painter.drawText(
                        int(rect.left() + 4), int(rect.top() + fm.ascent() + 3), line1,
                    )
                    if rect.height() > 36:
                        painter.drawText(
                            int(rect.left() + 4),
                            int(rect.top() + fm.ascent() + fm.height() + 3),
                            f"{pct:.0f}%",
                        )

        if self._subtitle:
            painter.setFont(font)
            painter.setPen(QPen(QColor("#6b7280")))
            painter.drawText(m, m + fm.ascent(), self._subtitle)
        if self._footnote:
            painter.setFont(font)
            painter.setPen(QPen(QColor("#92400e")))
            painter.drawText(m, self.height() - 4, self._footnote)
        painter.end()

    def _paint_empty(self, painter, message: str) -> None:
        font = QFont(painter.font())
        font.setPointSize(11)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#6b7280")))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(message)
        painter.drawText(int((self.width() - tw) / 2), int(self.height() / 2), message)

    def mouseMoveEvent(self, event) -> None:  # noqa: D401
        pos = event.position() if hasattr(event, "position") else event.posF()
        total = sum(t.value for t in self._tiles) or 1.0
        for rect, idx in self._hitmap:
            if rect.contains(pos):
                t = self._tiles[idx]
                pct = t.value / total * 100
                QToolTip.showText(
                    self.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))),
                    f"{t.name or t.label}\n{self._fmt(t.value)} · {pct:.1f}%",
                    self,
                )
                return
        QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: D401
        QToolTip.hideText()
        super().leaveEvent(event)
