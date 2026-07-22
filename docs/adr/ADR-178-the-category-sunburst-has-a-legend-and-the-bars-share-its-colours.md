# ADR-178 — The category sunburst has a legend, and the bars share its colours

**Date:** 2026-07-22
**Status:** Implemented
**Related:** ADR-068 (the Category & Payee report). ADR-134 (its roll-up levels — Top / Group / Leaf). ADR-067 (the two-ring donut, and the "no pies" exception). ADR-066 (the `PayeeChart` ranked bars). ADR-018 (no pies for time series). ADR-055 (the shared display-currency selector, unrelated but co-resident on the page). ADR-076 (theme repaint). `chart_helpers.colour_for` (the eight-slot categorical palette).

## Context

Owner-reported, looking at the Category & Payee page: the "Where it goes" sunburst had **no legend** — nothing on the page mapped colour → category. The ranked bars were a single flat teal, the table had no swatches, and the donut's only key was a hover tooltip. So proportions were readable but *which slice is what* required hovering each one. The other two `DonutChart` users already had a legend (`net_worth_window`, `income_expense_window`); Category & Payee added the chart bare — an oversight in ADR-134, not a decision.

## Decision

Two coupled changes, so the donut and the bars read as one picture.

**1. An inner-ring legend under the donut.** Swatch + name only, **no amounts** — the ranked bars and the table already carry the figures, so repeating them would be a third copy. The outer ring needs no entry of its own: its slices are tints of their parent's colour, so an inner-ring key explains the whole sunburst by family. Laid out in a **two-column grid**, filled down-then-across so reading order still runs largest-first down column one.

**2. The ranked bars share the sunburst's palette.** The donut owns the page palette — `_top_colours` (inner ring, top-level category) and `_group_colours` (outer-ring tints, budget-line group) — and the bars key off it via `_bar_colours(row_dim)`, which maps the current roll-up to the matching ring:

| Group by | Roll up | Bars take |
|----------|---------|-----------|
| Category | Top level | inner-ring colours |
| Category | Group *(default)* | outer-ring tints |
| Category | Leaf | flat accent |
| Payee | any | flat accent |

The fallbacks are load-bearing. The donut is deliberately always by category, independent of the primary dimension and the drill; payee bars and Leaf roll-up have no corresponding ring, so colouring them would invent a relationship that isn't there. `PayeeChart.render` gained an optional `colours` map (row id → colour); omitted or partial, each bar falls back to the single accent hue, so the standalone Payee report (`PayeeChart`'s only other caller — in fact its sole other user) is unaffected. This is a **categorical** key, not a rank signal; the ADR-066 "colour does not encode rank" promise is preserved as the default and the caller owns the correctness of the map it supplies.

**3. A first legend attempt that regressed the layout, then the fix.** The initial one-column legend stacked seven rows (~115px) against the donut's `stretch=1` + 200px floor, collapsing the donut to its minimum and eliding the centre total to `£3,451…`. Measured, not guessed. Fixed by: the two-column grid (115px → 64px); raising the donut floor to 260px; and an `_ElidedLabel` — a plain `QLabel` with an `Ignored` size policy *clips* mid-word (reads as a rendering bug), so the label now genuinely elides with the full name on the tooltip and reports `sizeHint().width() == 0` so a long category can never widen the summary panel.

## Rejected

- **A full leaf-level legend** (every outer slice). Thirty-odd rows would drown the panel and the signal; the inner-ring family key explains the chart at the right altitude.
- **Amounts in the legend rows.** A third copy of figures already six inches to the left in the bar chart and the table. Swatch + name is a colour key, not a table.
- **Colouring the payee bars, or category bars at Leaf roll-up.** No corresponding donut ring exists, so any colour would imply a mapping that isn't there. Flat accent is the honest answer.
- **A separate palette for the bars.** Two palettes for the same categories on one page is exactly the drift this avoids; the donut owns one palette and everyone reads from it.

## Consequences

- **The sunburst is now a readable panel, not a decorative one**, and the bar chart reinforces it: at the default Group roll-up, *Loan interest* renders orange against everything else's teal, because it sits under a different top-level root — information the flat-teal bars could not convey.
- The donut is rendered **before** the bars in `_rebuild_view`, because it populates the palette the bars consume. A one-line ordering dependency, commented at the call site.
- **Known, pre-existing caveat, unchanged by this work:** `colour_for(i)` assigns colour by rank (index into the size-sorted categories), which contradicts `chart_helpers`' own "colour must never follow rank". So if two categories swap size between periods they swap colours. Within a single render everything is consistent, so it doesn't affect the legend or the bar matching; it does mean colours aren't stable across periods. Recorded, not fixed — keying the palette to a stable category identity is a separate change.
- `PayeeChart` gained one optional keyword (`colours`); its default behaviour and the standalone Payee report are byte-for-byte unchanged.

Full suite 423 passed, 0 failed. No schema change.
