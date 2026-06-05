"""Horizontal proportional bar.

Single-row stacked bar — one segment per series, widths proportional to
each series' share of the total. Used by the Net Worth window to show
the asset-family split visually without resorting to a pie (owner rule).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


@dataclass(frozen=True)
class BarSegment:
    label: str
    amount: Decimal
    color: QColor


class ProportionalBar(QWidget):
    """Set `segments` then call `update()`; segments with non-positive
    amounts are skipped (zero-width segments would be invisible anyway
    and would clutter the divider-line drawing)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._segments: list[BarSegment] = []
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_segments(self, segments: list[BarSegment]) -> None:
        self._segments = [s for s in segments if s.amount > 0]
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 4, 0, -4)

        if not self._segments:
            painter.setBrush(QBrush(QColor("#e5e7eb")))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 4, 4)
            return

        total = sum((s.amount for s in self._segments), Decimal(0))
        if total <= 0:
            return

        # Draw each segment as a filled rectangle; the whole bar carries
        # one rounded clip so the ends are rounded without each segment
        # acquiring its own rounded corners.
        painter.save()
        painter.setClipRect(rect)
        # Background rounded rect (drawn first so any anti-alias gap on
        # the right edge falls onto the bg colour rather than the canvas).
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#e5e7eb")))
        painter.drawRoundedRect(rect, 4, 4)
        painter.restore()

        x = float(rect.left())
        for s in self._segments:
            width = float(s.amount / total) * rect.width()
            seg_rect = QRectF(x, rect.top(), width, rect.height())
            painter.setBrush(QBrush(s.color))
            painter.setPen(Qt.NoPen)
            painter.drawRect(seg_rect)
            x += width

        # Outer border — subtle.
        painter.setPen(QPen(QColor("#c7cbd1"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, 4, 4)
