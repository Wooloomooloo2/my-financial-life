# ADR-181 — The Investment Returns chart can fit its baseline to the data

**Date:** 2026-07-25
**Status:** Implemented
**Related:** ADR-046 (the Investment Returns report + `ReturnsChart`). ADR-026 / [[feedback-chart-engine-preference]] (hand-rolled `paintEvent`). ADR-076 (saved splitter/view state on report filters). ADR-119 (the page-header action row the toggle joins). ADR-066 (chart colour ≠ rank). ADR-159/165 (currency-aware chart formatting). Mirrors the zero-anchored y-range logic shared with `net_worth_history_chart`.

## Context

Owner-reported, with two screenshots. `ReturnsChart` (ADR-046) is a stacked-area composition — cost basis / gain-loss / realized / dividends — and its y-axis was always anchored at zero (`ymin = 0.0`, only extending *down* for a realized loss). On the "Max (all history)" window that reads perfectly: the portfolio grew from ~£0, so the stack fills the panel and the shape tells the story.

On a short window it fails. Over "Last 12 months" the whole stack floats between ~£950k and ~£1.5M, but the axis still runs 0 → £2M, so **all** the movement is crushed into the top quarter of the panel and the year looks flat — the growth, the September dip, the dividend band, all illegible. The owner's words: *"shorter snapshots of the graph look flat and don't really convey the movement or growth."*

This isn't a bug — it's a deliberate, app-wide convention. `chart_helpers.nice_ticks` only ever computes an axis *max*; every chart in the app pins its floor at zero. The convention is right for most of them. It's wrong for a report whose entire job is showing how a large, long-established portfolio moves over a chosen period.

The tension is real and specific to a **filled area** chart. On a zero baseline, each band's *height* equals its true magnitude — the blue really is the whole cost basis. Lift the floor to £900k to magnify the movement and the blue band gets sliced off at the bottom: an area meant to say "£1M of cost" becomes a thin sliver. That truncation is standard and harmless for a *line* chart (nobody reads a line's area), but for a composition of filled areas it can mislead — especially for the non-technical people the owner shares this app with.

## Options considered

Put to the owner with mock previews:

**A. Fit the baseline to the data, with a Zero/Fit toggle (chosen).** Default to Fit so the complaint is fixed out of the box; a toggle returns to the honest zero-anchored composition. Fixes the common case, keeps the rigorous view one click away.

**B. Always fit, no toggle.** Simplest, but permanently discards the true-magnitude composition — the blue is always truncated with no way back.

**C. In fit mode, drop the fills and draw cost / value / total as lines.** A non-zero baseline is honest for lines. But it's a different chart from the stacked "all time" view, and loses the composition-area reading the report is built around.

The owner chose **A**.

## Decision

**`ReturnsChart` gains a fit/zero y-axis mode, defaulting to fit, surfaced as a "Fit axis" toggle in the report header and persisted per saved report.**

**The y-range (`ReturnsChart._y_range`).** Zero mode is the original behaviour, byte-for-byte: floor at 0, extend downward in whole steps only for a realized loss that pushes a stack below zero. Fit mode brackets `[data_lo, data_hi]` on round numbers via a new `chart_helpers.nice_bounds` (the non-zero-floor sibling of `nice_ticks`):

- `data_hi` = the tallest stack top.
- `data_lo` = the **lowest cost-band top** across the window (`min(cost_low)`), *not* zero. The blue below that line is the same solid base at every sample — the dead space the owner wanted reclaimed — while the part of the cost band that actually varies stays on-panel. It drops below zero only when a realized loss does (the same excursion zero-mode handles).
- 8% padding, then `nice_bounds` rounds the floor down and the ceiling up to a `{1,2,5}×10ⁿ` step.

Fit **reduces to zero-anchored automatically** when the window reaches back to a near-empty portfolio (`data_lo ≈ 0`), so "Max" looks identical in either mode — the change is invisible exactly where the old behaviour was already right.

**Honesty under truncation, two ways.** The y-axis labels already read the real values (the bottom label says "£800,000", not "£0"), which is the standard non-zero-axis signal. On top of that, when fit lifts the floor above zero the chart draws a small **zig-zag axis-break glyph** at the foot of the y-axis — the conventional "this axis is truncated" mark — so a filled band clipped at the bottom isn't misread as its full size. In zero mode there's no glyph.

**Clipping.** With the floor above zero the cost band runs off the bottom of the plot; `paintEvent` now clips the filled composition (bands + cost line + zero baseline) to the plot rect so it can't paint over the x-axis labels.

**Persistence & UI.** A `chart_fit: bool = True` field on `InvestmentReturnsFilters` (a view preference alongside the ADR-076 splitter sizes; an old blob with no key auto-upgrades to `True`). A checkable "Fit axis" ghost button in the page header toggles it: it re-renders the chart in place (no recompute — the mode only changes the axis) and marks the report dirty so a Save keeps the choice. Because the filter dialog builds a fresh `InvestmentReturnsFilters` (defaulting `chart_fit` back on), `_on_open_filter` now carries the view-state fields — `chart_fit` and both splits — across an edit rather than letting the dialog reset them.

**Checkable-ghost styling.** The `ghost` button variant had no `:checked` style, so a checkable ghost looked identical on and off. Added a `:checked` rule mirroring the checked header tool-buttons (the `accent_subtle` fill), so the toggle's state is legible. General fix; benefits any future checkable ghost.

## Consequences

- The reported problem is fixed by default: any short window now fills the panel and shows its movement, while "Max" is unchanged.
- A viewer who wants the true-magnitude composition clicks the toggle off; the choice saves with the report.
- `nice_bounds` is a small, tested, reusable helper — the first non-zero-baseline axis in the app, available to any future chart that needs one.
- The area-truncation caveat is mitigated (real axis labels + the break glyph) but not abolished — a filled band on a lifted floor is inherently a partial view, which is why zero mode stays one click away rather than being removed.
- Only `ReturnsChart` changes. The other charts keep the zero convention; none reads `nice_bounds` yet.

## As built

- `mfl_desktop/ui/chart_helpers.py` — new `nice_bounds(vmin, vmax, target_count=5) -> (min, max, step)`.
- `mfl_desktop/ui/returns_chart.py` — `_fit` state + `set_fit`; `_y_range` (zero vs fit); `paintEvent` clips the fill and draws `_paint_axis_break` when the floor is lifted.
- `mfl_desktop/reports/filters.py` — `InvestmentReturnsFilters.chart_fit: bool = True`.
- `mfl_desktop/ui/investment_returns_window.py` — "Fit axis" toggle, `_on_toggle_fit`, and view-state carry-over in `_on_open_filter`.
- `mfl_desktop/ui/theme.py` — `:checked` style for the ghost button variant.
- `tests/test_returns_chart_fit_baseline.py` — 14 tests (nice_bounds; both y-range modes incl. the realized-loss floor and the reduce-to-zero case; round-trip + legacy-blob upgrade; the toggle wiring and the preserve-across-dialog path).

Verified offscreen against the live portfolio: 12-month **fit** runs £600k → £1.6M with the year's movement legible and the break glyph shown; 12-month **zero** reproduces the original 0 → £2M; **Max fit** is zero-anchored (no glyph). Full suite green. No schema change (the flag rides in the existing `filters_json`).
