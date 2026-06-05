"""Compact burn-down chart for the budget screen.

Two line series — Actual cumulative outflow vs. the linear Ideal pacing
line — plus a vertical marker for today. Sits below the summary tiles
on the budget window; not its own report. QtCharts (already on the
PySide6 dependency, no extra package).
"""
from __future__ import annotations

from PySide6.QtCharts import (
    QChart,
    QChartView,
    QLineSeries,
    QValueAxis,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QFrame, QSizePolicy, QVBoxLayout, QWidget

from mfl_desktop.budget_calc import BurnDownData


class BurnDownChart(QWidget):
    """Stateless widget — call ``set_data(burn_down)`` to render."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._chart = QChart()
        self._chart.setAnimationOptions(QChart.NoAnimation)
        self._chart.setMargins(self._chart.margins())   # default margins
        self._chart.legend().setVisible(True)
        self._chart.legend().setAlignment(Qt.AlignBottom)
        self._chart.setTitle("")

        self._view = QChartView(self._chart)
        self._view.setRenderHint(QPainter.Antialiasing)
        self._view.setFrameShape(QFrame.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(220)
        self.setMaximumHeight(260)

    def set_data(self, data: BurnDownData) -> None:
        """Replace the chart's series + axes from a BurnDownData snapshot."""
        # Clear previous state — QChart's API is happiest when we drop
        # all axes + series each refresh rather than diff them.
        self._chart.removeAllSeries()
        for ax in list(self._chart.axes()):
            self._chart.removeAxis(ax)

        actual_series = QLineSeries()
        actual_series.setName("Actual")
        pen_actual = QPen(Qt.SolidLine)
        pen_actual.setColor(Qt.GlobalColor.darkRed)
        pen_actual.setWidth(2)
        actual_series.setPen(pen_actual)

        ideal_series = QLineSeries()
        ideal_series.setName("Ideal")
        pen_ideal = QPen(Qt.DashLine)
        pen_ideal.setColor(Qt.GlobalColor.darkGray)
        pen_ideal.setWidth(2)
        ideal_series.setPen(pen_ideal)

        for x, a, i in zip(data.x_days, data.actual, data.ideal):
            actual_series.append(x, float(a))
            ideal_series.append(x, float(i))

        self._chart.addSeries(actual_series)
        self._chart.addSeries(ideal_series)

        x_axis = QValueAxis()
        x_axis.setLabelFormat("%d")
        x_axis.setTitleText("Day of period")
        if data.x_days:
            x_axis.setRange(data.x_days[0], data.x_days[-1])
            tick = max(1, len(data.x_days) // 6)
            x_axis.setTickCount(min(8, len(data.x_days) // tick + 1))

        y_axis = QValueAxis()
        y_axis.setLabelFormat("%.0f")
        y_axis.setTitleText("Cumulative outflow")
        # Range needs a sensible max even with zero data — fall back to 1
        # so the chart isn't a single horizontal line at 0.
        max_y = max(
            float(data.total_planned),
            max((float(v) for v in data.actual), default=0.0),
            1.0,
        )
        y_axis.setRange(0.0, max_y * 1.1)

        self._chart.addAxis(x_axis, Qt.AlignBottom)
        self._chart.addAxis(y_axis, Qt.AlignLeft)
        actual_series.attachAxis(x_axis)
        actual_series.attachAxis(y_axis)
        ideal_series.attachAxis(x_axis)
        ideal_series.attachAxis(y_axis)

        # Today marker — vertical line at today_day. Implemented as a
        # short two-point series so it stays consistent with the rest of
        # the chart's coordinate system (no overlay painting needed).
        if 1 <= data.today_day <= (data.x_days[-1] if data.x_days else 0):
            today_series = QLineSeries()
            today_series.setName(f"Today (day {data.today_day})")
            pen_today = QPen(Qt.DotLine)
            pen_today.setColor(Qt.GlobalColor.darkBlue)
            pen_today.setWidth(1)
            today_series.setPen(pen_today)
            today_series.append(data.today_day, 0.0)
            today_series.append(data.today_day, max_y * 1.1)
            self._chart.addSeries(today_series)
            today_series.attachAxis(x_axis)
            today_series.attachAxis(y_axis)
