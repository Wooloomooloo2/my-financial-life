"""Period presets + date-range maths — the single source of truth (ADR-082).

Pure Python, **no Qt, no SQL** (sits beside ``account_summary``/``fx``/``holdings``
in the compute layer, so the Qt-free dialogs, windows, and the CLI can all share
it). Before this module the period vocabulary was scattered: the register had its
own preset list + ``_months_before``; ``account_summary`` had ``period_bounds`` +
``PERIOD_LABELS``; ``reports/filters`` had three more key tuples; and four report
windows each carried an inline ``_resolve_bounds`` and a copy of ``_PERIOD_LABELS``.
This consolidates all of it.

Two things stay deliberately *un*-consolidated because they are legitimately
different per context — and because their keys are **persisted in saved-report
``filters_json``**, so they must never change:

* the **preset SETS** (register / report / investment / sankey each offer a
  different menu), and
* nothing else — every key resolves through the one :func:`period_bounds` and the
  one :data:`PERIOD_LABELS` registry below.

Month/year windows are **calendar-accurate** (``months_before`` — "6 months ago"
lands on the same day-of-month, not today − 180 days), which is what the register
always did; the report windows previously used day-deltas and now align to this
(a 0–3 day shift on their rolling windows — see ADR-082). Day windows
(``30d``/``90d``/``quarter``) stay rolling-by-days by design.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Optional


# ── canonical label registry (the ONLY copy) ───────────────────────────────
PERIOD_LABELS: dict[str, str] = {
    "30d":        "Last 30 days",
    "90d":        "Last 90 days",
    "quarter":    "Last Quarter",
    "6m":         "Last 6 months",
    "12m":        "Last 12 months",
    "1y":         "Last 12 months",
    "ytd":        "Year to date",
    "mtd":        "Month to date",
    "last_month": "Last month",
    "3y":         "Last 3 years",
    "5y":         "Last 5 years",
    "max":        "Max (all history)",
    "all":        "All",
    "custom":     "Custom",
}

# ── preset SETS per context ─────────────────────────────────────────────────
# The keys are persisted in saved-report filters_json — DO NOT change them.
REGISTER_PRESETS:   tuple[str, ...] = ("30d", "90d", "6m", "12m", "ytd", "all")
REPORT_PRESETS:     tuple[str, ...] = ("quarter", "6m", "ytd", "1y", "3y", "custom")
INVESTMENT_PRESETS: tuple[str, ...] = ("ytd", "1y", "3y", "5y", "max", "custom")
SANKEY_PRESETS:     tuple[str, ...] = ("ytd", "mtd", "last_month", "6m", "1y", "custom")

DEFAULT_REGISTER_KEY = "12m"

# Number of calendar months for each month/year key.
_MONTHS = {"6m": 6, "12m": 12, "1y": 12, "3y": 36, "5y": 60}
# Number of days for each rolling-day key.
_DAYS = {"30d": 30, "90d": 90, "quarter": 90}

_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def months_before(d: date, months: int) -> date:
    """Calendar-accurate "N months ago" — 'Last 12 months' is the same day last
    year, not ``d − 365`` days. The day is clamped to the target month's last
    valid day (e.g. 31 Mar − 1 month → 28/29 Feb)."""
    total = d.year * 12 + (d.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def period_bounds(
    key: str,
    today: date,
    *,
    earliest: Optional[date] = None,
    custom_start: Optional[date] = None,
    custom_end: Optional[date] = None,
) -> tuple[Optional[date], date]:
    """Resolve a preset key to ``(start, end)``.

    ``start`` is ``None`` only for the unbounded ``all`` / ``max`` keys when no
    ``earliest`` is supplied (the register reads this as "no lower bound");
    every other key returns a concrete start date. ``custom`` requires both
    ``custom_start`` and ``custom_end`` and raises otherwise, so a window that
    forgot to collect dates fails loudly rather than silently using today.
    """
    if key == "custom":
        if custom_start is not None and custom_end is not None:
            return custom_start, custom_end
        raise ValueError(
            "period_bounds: 'custom' needs explicit custom_start/custom_end."
        )
    if key in ("all", "max"):
        return earliest, today            # earliest may be None → unbounded
    if key == "ytd":
        return date(today.year, 1, 1), today
    if key == "mtd":
        return date(today.year, today.month, 1), today
    if key == "last_month":
        last_prev = date(today.year, today.month, 1) - timedelta(days=1)
        return date(last_prev.year, last_prev.month, 1), last_prev
    days = _DAYS.get(key)
    if days is not None:
        return today - timedelta(days=days), today
    months = _MONTHS.get(key)
    if months is not None:
        return months_before(today, months), today
    raise ValueError(f"Unknown period key: {key!r}")


def period_since(
    key: str, today: date, earliest: Optional[date] = None,
) -> Optional[str]:
    """Convenience for the register's windowed load: the ISO ``start`` of the
    preset, or ``None`` for unbounded (``all``). Never raises on ``custom`` —
    callers that don't use custom presets pass register keys only."""
    if key in ("all", "max") and earliest is None:
        return None
    start, _ = period_bounds(key, today, earliest=earliest)
    return start.isoformat() if start is not None else None


def fmt_date_range(start: date, end: date) -> str:
    """Compact human range — ``1 Jun → 6 Jun`` when years agree, else
    ``1 Jun 2025 → 6 Jun 2026``. Avoids ``%-d`` (Windows pitfall)."""
    smonth = _MONTH_ABBR[start.month - 1]
    emonth = _MONTH_ABBR[end.month - 1]
    if start.year == end.year:
        return f"{start.day} {smonth} → {end.day} {emonth}"
    return (
        f"{start.day} {smonth} {start.year} → {end.day} {emonth} {end.year}"
    )


def period_label(
    key: str,
    custom_start: Optional[date] = None,
    custom_end: Optional[date] = None,
) -> str:
    """Human label for a period selection. ``custom`` with both bounds renders
    ``Custom: 1 Jun → 6 Jun``; without them it falls back to bare ``Custom``."""
    if key == "custom" and custom_start is not None and custom_end is not None:
        return f"Custom: {fmt_date_range(custom_start, custom_end)}"
    return PERIOD_LABELS.get(key, key)


def labels_for(keys: tuple[str, ...]) -> dict[str, str]:
    """The label sub-registry for one preset set (e.g. ``REPORT_PRESETS``)."""
    return {k: PERIOD_LABELS[k] for k in keys}


def options_for(keys: tuple[str, ...]) -> list[tuple[str, str]]:
    """``[(label, key), …]`` in preset order — ready for a combo's ``addItem``."""
    return [(PERIOD_LABELS[k], k) for k in keys]
