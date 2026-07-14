# ADR-166 — A validated, on-brand categorical chart palette

**Date:** 2026-07-14
**Status:** Implemented
**Related:** ADR-161–165 (the design-review round). ADR-100 (the brand re-tone to teal). ADR-076 (design tokens, light + dark). ADR-026 (the hand-rolled paintEvent chart engine). ADR-128/157 (segment gaps, bar totals — the "secondary encoding" this palette leans on).

## Context

From the design review, and the last item in it. The app's identity is a deep teal; the charts were painted in saturated Tailwind primaries — blue, emerald, amber, red, violet, cyan, pink, lime — that read as a library default and fought the chrome around them.

That was the *cosmetic* complaint. Running the palette through a validator (Machado-2009 CVD simulation, OKLCH lightness/chroma, contrast) found the real one. `GROUP_PALETTE` carried this comment:

> `# Stable stack colours … Tested at AA contrast against white at 9pt typography.`

**It had not been tested.** Measured against white:

- **violet-500 (`#8b5cf6`) vs blue-600 (`#2563eb`): ΔE 3.3 under protanopia.** For a red-blind reader — roughly 1 in 12 men — the first and fifth categories in every stacked bar, every treemap and every sunburst were *the same colour*.
- **Four of the twelve sat below 3:1 contrast** on white (emerald 2.54, amber 2.15, cyan 2.43, lime 1.98).
- The palette **cycled** with `index % len`, so a 13th series silently became a second blue.

A palette that is inaccessible is not a matter of taste, and the comment asserting the opposite is how it survived this long.

## Decision

**Eight slots, fixed order, validated, theme-aware, and never cycled.**

The values are not eyeballed. Both columns were produced by iterating against the validator until every check passed in **all-pairs** mode — the standard the sunburst and treemap need, where any two slices can end up adjacent (the default adjacent-only check would have hidden exactly the violet/blue collapse above).

| | slot 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| **light** | `#0d9488` teal | `#d99000` amber | `#2a78d6` blue | `#008300` green | `#e34948` red | `#4a3aa7` indigo | `#dd6699` pink | `#eb6834` orange |
| **dark** | `#0d9488` | `#c88400` | `#2a78d6` | `#008300` | `#e34948` | `#5f4dbf` | `#dd6699` | `#e3612e` |

Three findings worth keeping:

1. **The brand accent cannot be a series colour.** `#1f6e78` has OKLCH chroma **0.075**, below the 0.10 floor — as a large fill it reads *grey*. Slot 1 is the same hue at usable chroma (`#0d9488`). The accent still owns the chrome; it just can't own data.
2. **Dark is a separately validated set, not a lightened light one.** Three slots had to move to stay inside the dark lightness band (0.48–0.67, which is *narrower and lower* than the light band, not higher — the first instinct, to brighten everything, put six of eight out of band).
3. **Blue and indigo are held apart by lightness, not hue.** Under deuteranopia they *are* the same hue; the only separation is the lightness gap. Closing it — which is what a naive "brighten the indigo for dark mode" does — collapses them to ΔE 0.6, reintroducing the exact defect being fixed. The dark surface pushes back (a dark indigo has poor contrast on `#1e293b`), and the accepted compromise is a CVD floor-band ΔE 8.4 in dark, legal *only* because of the secondary encoding below.

**Secondary encoding is what makes the floor-band pairs legal**, and the charts already have it: every series carries a text label in the legend, stacked segments are separated by a 2 px surface gap (ADR-128), bars print their totals (ADR-157), and hovering names the segment. Identity is never colour-alone. This is a **dependency, not a coincidence** — if a future chart drops the legend, its palette is no longer accessible.

**The palette does not cycle.** `colour_for` is now the only reader, and it resolves from tokens at *paint* time, so a theme toggle recolours every chart with no per-chart wiring.

## The ninth category — caught by looking, not by validating

The validator passed, and the first render was still wrong: the demo file has **nine** top-level expense categories, and "Charity and gifts" came out in exactly Housing's teal. A false identity match — worse than the old palette, which had twelve colours before repeating.

The comment I had just written ("eight is more than these charts can be read at") was wrong on the very first chart it met.

So Spending / Income Over Time now **keep the top seven groups and fold the rest into a single, labelled "Other"** — a synthetic negative group id (`-200`), guarded in the click handlers exactly like ADR-110's Reinvested Dividends, because "Other" is not a category and has nothing to drill into. The tail is *folded*, never dropped: the bar totals still reconcile.

## Rejected

- **Keeping twelve colours.** Twelve hues cannot be made CVD-distinct; the extra four are the ones that collapse. Eight that work beat twelve that don't.
- **Generating a 9th hue** (rotating hue, or lightening slot 1). It produces a colour that is by definition not validated, and it re-creates the collapse the fixed order exists to prevent.
- **Letting the accent be slot 1.** It fails the chroma floor. Wanting the brand colour in the chart is not a reason to put a grey-reading fill in it.
- **Auto-deriving dark from light** (lighten by a fixed step). Six of eight land outside the dark band; two collapse.

## Consequences

- **Every chart changes colour.** Stacked bars, the Sankey, the treemap, the sunburst, Income & Expense, Category & Payee — all read `colour_for`.
- A chart with more than eight categories now shows an **"Other"** slice where it previously showed a ninth (duplicate-coloured) category. Totals are unchanged.
- Dark mode gets its own series values for the first time; charts previously used the light hexes on the dark surface.
- The floor-band CVD pairs (light 11.0, dark 8.4) are legal **only while the legend and labels exist**. Stated as an invariant, not left implicit.

## Known limitations — recorded, not fixed

- **Only Spending / Income Over Time fold to "Other".** The **treemap, sunburst, Sankey and Category & Payee can still exceed eight series**, and there `colour_for` wraps — so a 9th slice repeats slot 1's teal, the very bug fixed above. The Sankey has its own "Hide below %" → Other and the sunburst has a top-group rollup, which *mitigate* but do not guarantee it. **Each needs the same fold.** Filed in the backlog.
- **Colour follows rank, not entity.** Groups are ordered by total, so slot 1 is "the biggest category", and a filter that removes the largest one **repaints the survivors**. The dataviz rule is that colour should follow the entity. Fixing it needs a stable category→slot map (and a policy for >8 distinct entities), which is a bigger change than this one. Filed.

`tests/test_series_palette.py` 8/8 + `tests/test_spending_other_fold.py` 5/5 — including a guard that the fixture really has more categories than slots, so the fold tests can't pass vacuously. Full suite 344/344. No schema change.
