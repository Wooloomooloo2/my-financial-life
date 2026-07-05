# ADR-135 — Net Worth over time: custom range, granularity, and stacked bars

**Date:** 2026-07-05
**Status:** Implemented
**Related:** ADR-121 (historical net worth — the view this amends). ADR-026 (hand-rolled `paintEvent` charts). ADR-018 (no pies — bars honour it). ADR-055 (FX convert-before-sum). ADR-082/084 (period + granularity vocabulary). ADR-128 (rounded bar corners — a later polish target).

## Context

Owner cleanup asks for the Net Worth **Over time** view (ADR-121): bring it "in line with the other over-time reports" by allowing a **custom range** and a **granularity** selector, and change the chart from a **continuous line** to a **bar-style** chart. The view shipped with a fixed period combo (1y / 3y / 5y / All), month-only sampling, and a stacked-**area** composition topped by a net-worth **polyline**.

## Decision

Add the two controls and re-shape the chart to stacked bars with a per-bar net marker.

- **Sampling by granularity + custom range.** New pure helpers in `net_worth_history.py`: `period_end_samples(start, end, granularity)` (weekly steps of 7 days; monthly/quarterly/annually keep calendar month-ends — all / Mar-Jun-Sep-Dec / Dec; start and end always sampled so the series spans the exact range) and `resolve_history_granularity(start, end, "auto")` (span → bucket: ≤4mo weekly, ≤3y monthly, ≤7y quarterly, else annually — so a short range samples finely and a decade doesn't produce hundreds of bars). `month_end_samples` is kept (its ADR-121 tests + semantics are unchanged).
- **Window controls.** The Over-time page gains a **Custom…** period option (with From/To `QDateEdit`s shown only for Custom) and a **Granularity** combo reusing the shared `GRANULARITY_OPTIONS` (Auto / Weekly / Monthly / Quarterly / Annually) — the same vocabulary as Spending / Income / I&E. `_history_bounds()` resolves the preset (custom reads + swaps the pickers; `all` starts at the earliest transaction; rolling presets are whole-year day-deltas); `_history_sample_dates()` resolves `auto` then calls `period_end_samples`.
- **Bars, not a line.** `NetWorthHistoryChart` is re-shaped (ADR-026 paintEvent) from stacked-area + polyline to **one stacked bar per sample** — asset families up from zero, debt families down — with a **net-worth dot on each bar** (white casing + ink fill for legibility over any family colour). This was the owner's pick ("stacked bars + net marker") over bars-only or single net-worth bars, keeping both the composition and the bottom line readable. `_x_for` becomes a bar-slot centre (each sample owns an equal slot, bars centred with axis margins, x-labels under their bars); the family colours, y-axis/gridlines/zero-baseline, legend, and hover tooltip are retained. The dead area/line code (`_paint_bands`, `_paint_net_line`, the cumulative-quad polygons, `QPolygonF`) is removed.

Rejected: bars-only with no net overlay (owner wanted the bottom line visible); a single net-worth bar per period (drops the family composition the view exists to show); a new persisted granularity default (the Over-time view's period/granularity are session view-state, not saved-report filters — nothing to migrate).

## Consequences

- The Over-time view now matches the other reports' period + granularity ergonomics and reads as discrete bars with a clear net-worth dot per period.
- Weekly granularity over long spans produces many thin bars; `auto` avoids that by escalating the bucket with the span, and the bar width clamps to ≥2 px, so density stays legible.
- Pure of Qt in the compute layer; view + window only, no schema change, no migration.
- Follow-up (polish arc): apply ADR-128 rounded bar corners to these bars for visual consistency with the other report charts.
- Verified headless (offscreen, live `mfl_dev.mfl`): samplers span start→end and count sensibly per granularity; `auto` resolves as specified; the Over-time page renders (`grab()`); Custom pickers show only for Custom; a granularity change re-renders. `test_net_worth_history.py` (the ADR-121 helpers) still 4/4; full self-running suite green.
