# ADR-128 — Consistent rounded bar corners across report charts

**Date:** 2026-07-01
**Status:** Implemented
**Related:** ADR-026 (hand-rolled paintEvent charts + `chart_helpers` — where the shared bar helper now lives). ADR-076 (light/dark tokens — the carve fills the plot-background token). Affects the Spending Over Time (`spending_chart.py`), Income & Expense (`income_expense_chart.py`), and Investment Income (`investment_income_chart.py`) report charts.

## Context

Owner report: on the Spending Over Time report some bars have a nice rounded top while others have a dated square top, inconsistently from bar to bar within the same chart.

Cause: each bar chart rounded its top by drawing the **topmost segment** as a rounded-top `QPainterPath`, but only when that segment was tall enough — `spending_chart` guarded on `is_top and rect.height() > radius * 1.4`, and `income_expense_chart` had the same `rect.height() < radius * 1.4 → fillRect` fallback. So a bar whose top segment was a thin cap (a few pounds of "Misc" sitting on a tall stack) fell through to a **square** `fillRect`, while a bar with a tall top segment rounded. The rounding was a property of the top segment's height, not of the bar — which is exactly why it looked random. A per-segment rounded path *can't* round a cap thinner than the radius (the corner arc would need more vertical room than the segment has), so the guard was load-bearing, not incidental. Separately, `investment_income_chart` never rounded at all (plain `fillRect`), and the three charts used three different radius formulas (`min(6, w/4)`, `min(5, w/3)`, none) — so even the rounded ones didn't match across reports.

## Decision

Round the corners of the **whole bar**, not a segment, with one shared radius, so every bar curves identically regardless of how its stack is composed — and unify the three charts on it.

New shared helpers in `mfl_desktop/ui/chart_helpers.py`:

- `BAR_CORNER_RADIUS = 6.0` and `bar_corner_radius(bar_w) → min(6, bar_w/3)` — the single radius every report bar uses (clamped so a narrow bar never over-rounds).
- `round_bar_corners(painter, rect, radius, bg, *, top, bottom)` — rounds a **just-drawn** bar by filling its corner wedges with the plot-background colour `bg`.

**Carve, not clip or per-segment fill.** Filling the corner wedges with the background (rather than drawing a rounded fill or clipping) is what makes this work uniformly:
- it composes over an already-painted stack of segments without needing each segment's colour (the cap may be one colour or several);
- it works at **any height** — a thin cap rounds exactly like a tall bar, because the carve is applied to the full bar rect, not the cap;
- it stays **crisp** under antialiasing — a filled `quadTo` arc, unlike `QPainter.setClipPath`, which produces a 1-bit (aliased, stair-stepped) rounded edge.

Wiring:
- **spending** (stacked): draw every segment as a plain rect (+ the existing 1px separators), then carve the two top corners of the full bar once.
- **income_expense**: fill each bar, then carve — `top` for the up-growing income bar, `bottom` for the down-growing expense bar (rounding the *outer* end, baseline end square).
- **investment_income**: fill each bar, then carve the top corners — previously square.

Rejected: (a) clamping the per-segment radius to the cap height — thin caps would round *less* than tall bars, i.e. still inconsistent; (b) `setClipPath` a rounded-top bar path — semantically clean but aliased corners, a visible regression for a change whose whole point is that it "looks nice"; (c) leaving each chart its own radius — the cross-report mismatch was part of the complaint.

The horizontal **payee** chart already rounds uniformly via `drawRoundedRect` (all four corners, its natural look for horizontal bars) and is left as-is — a different orientation, not part of the inconsistency.

## Consequences

- Every vertical report bar now has the same rounded top (income/expense: rounded outer end), independent of segment composition or bar height. View layer only; no data, no migration.
- One radius + one routine shared by three charts; three ad-hoc rounding blocks and two divergent radius constants removed (`spending._draw_bar_segment` rounding branch, `income_expense._draw_rounded_rect`, `_BAR_RADIUS_MAX`). Now-unused `QPainterPath` imports dropped.
- Minor, accepted: the carve paints background over a ≤6px corner at each bar's apex; if a gridline happened to sit exactly at a bar's top it could show a hairline gap there. Bars sit below the top gridline (axis max = `nice_ticks(vmax·1.12)`), so in practice this never lands on a gridline; the average/zero reference lines are drawn *after* bars and are unaffected.
- New `tests/test_bar_corners.py` (7/7, pixel-level): asserts the corner pixel becomes background while the top-edge centre stays bar-coloured, **including a height-4 cap** (the regressing case), plus the radius clamping. Verified visually across all three reports in light and dark.
- Future bar charts call the same two helpers for a matching look.
