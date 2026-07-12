# ADR-154 — Stacked-bar charts print each bar's total above it

**Date:** 2026-07-12
**Status:** Implemented
**Related:** ADR-026 (Spending Over Time chart; hand-rolled paintEvent engine). ADR-018 (no pies). ADR-128 (shared bar corner radius). ADR-114 (segment double-click → transactions). ADR-134 (category roll-up levels). ADR-064 (the *other* chart — Income & Expense combo).

## Context

Owner report, looking at the Income Over Time report ("Passive Income", 12 annual buckets, stacked by category): the totals of each column should be visible.

They are right, and the reason is worth stating precisely. **A stacked bar's headline quantity is its total** — comparing segments across stacks is already hard (only the bottom series shares a baseline), so the number the eye is actually reaching for is the height of the whole bar. That was the one number the chart never showed:

- The summary rail gives a **grand total** (£294,993.45) and an **average** (£24,582.79 / year).
- Hovering a segment gives **that segment's** value.
- The per-bucket total — what each bar *is* — was obtainable only by eyeballing gridlines **£20,000 apart**, or by hovering all four segments and adding them up by hand.

So the chart showed the aggregate of all bars and the value of any one slice of one bar, but not the bar. In a personal-finance app, "somewhere north of £70k" is not an acceptable answer to "what did 2025 come to?"

Two things made this an easy call rather than a judgement one:

- **The value already exists.** `SpendingChart._paint_bars` already sums each stack (`running`) and already tracks its top edge (`bar_top_y`) to round the corners (ADR-128). This is not new computation; it is promoting a number from transient-on-hover to always-visible.
- **The chart already draws value text on the canvas.** `_paint_average` renders the `Avg £24,583` pill. The idiom and the tokens were already there to match.

Note the widget is shared: `income_report_window` subclasses `spending_report_window`, so both **Income Over Time** and **Spending Over Time** use `SpendingChart` and both get this. That is the right outcome — the argument is about stacked bars, not about income.

One thing that *did* need checking rather than assuming: whether negative bars were a case to handle. They are not. `_paint_bars` only emits a segment `if pence > 0`, so every bar in this chart grows up from zero. (The Income & Expense combo chart — `IncomeExpenseChart`, ADR-064 — *does* draw below the baseline, but it is a different widget and is out of scope here.)

## Decision

**Print each stack's total above its bar, in the chart's ink at 9pt bold** — matching the weight of the average pill, since these are headline values rather than axis furniture.

Three details carry the design:

### Suppress wholesale, never partially

Granularity on these reports goes down to **daily**, and the period can span years — so a chart can legitimately have hundreds of buckets whose labels would collide into unreadable mush. `_layout_bar_totals` measures the **widest** label and returns `[]` if it doesn't fit its slot (`slot_w < widest + 8`).

It is all-or-nothing on purpose. Labelling only the bars that happen to have room would make the chart look like it had labelled an arbitrary subset — the reader can't tell "no room" from "no data". When the labels are off, the hover tooltip still carries every number, which is what it was already for.

### Lay the totals out first; draw them last

The totals are **data**; the average pill is **annotation**. When they collide, the annotation moves.

They collide more often than one might guess: the final bucket is typically a **part**-period (2026 is three months in), so the last bar tends to land near the average — which is exactly where the pill lives, at the right-hand end of the dashed line. In the owner's real screenshot the 2026 bar top sits almost exactly on the average line.

So `paintEvent` now runs `_layout_bar_totals` (pure geometry, no painting), hands those rects to `_paint_average` as `avoid=`, and only then paints the labels:

- `_paint_average` places its pill above the line as before; if that rect intersects any total label, it drops the pill **below** the line instead. Its dark fill (`chart_tooltip_bg`) keeps it legible over a bar.
- The labels are painted **after** the average line, so a total that sits on the dashed line isn't struck through by it.

Painting order alone turned out not to be enough, which only showed up against the owner's real data: a bar whose top lands *just under* the average line puts its label directly on the dashes, and a dashed line still shows through the gaps **between glyph strokes** — it reads as a strike-through even though the text is on top. So each label first knocks its own rect out to the chart surface colour before drawing (a halo). A label can never overlap a neighbouring bar — it is centred on its own bar and constrained to its own slot — so clearing to the surface colour is always correct, and it cleans up gridlines behind the text as a side benefit.

### Totals only, not per-segment

Four numbers stacked inside the 2025 bar would be noise, and the thin segments in the early years (2015–2020, some only a few hundred pounds tall) have no room for text at all. The segment breakdown is what the hover tooltip and the legend are for.

## Rejected

- **Per-segment value labels.** Unreadable in thin segments, cluttered in tall ones, and it answers a question the tooltip already answers well.
- **Label only the tallest bar, or only bars above the average.** Arbitrary; the reader can't distinguish "not labelled" from "no data", and it makes the chart look broken rather than considered.
- **Drop the labels per-bar when a given one doesn't fit.** Same objection, in its worst form — a chart with a ragged, apparently random subset of labels.
- **Put the totals in the summary rail as a table.** Duplicates the chart, doesn't scale past a handful of buckets, and doesn't answer "which bar is that?" — the whole value here is the number being *next to* the thing it describes.
- **Move the average pill permanently below the line.** Fixes the collision but pointlessly worsens the common case, where above-the-line is the cleaner spot.
- **Suppress the average pill when it collides.** Loses information to preserve information. Moving it costs nothing.

## Consequences

- Every bar on Income Over Time and Spending Over Time now carries its total, so the report answers "what did 2025 come to?" at a glance instead of via arithmetic on four tooltips.
- The average pill relocates below the dashed line when the last bar's total would otherwise sit on top of it. Verified on the owner's real data shape: `£25,000` above the 2026 bar, `Avg £24,583` immediately below the line, neither obscured.
- At monthly/daily granularity the labels vanish entirely and the chart looks exactly as it does today. Verified at 144 buckets.
- `_paint_average` gained an optional `avoid` parameter; it defaults to `None`, so nothing else that calls it changes behaviour.
- **Pre-existing, deliberately not fixed here:** `SpendingChart` hard-codes `£`. `fmt_currency`'s default symbol is used for the y-axis labels, the average pill, the tooltip — and now the bar totals. A user whose display currency is USD or EUR sees pound signs throughout this chart. That is a real bug, it predates this ADR, and it wants its own change (thread the report's display currency into `render()`, as `IncomeExpenseChart` already does via its `symbol=` argument). Flagged here because this ADR adds a fourth place that will need updating.
- 7 new tests (`tests/test_spending_chart_bar_totals.py`) covering one-label-per-bar, the label equalling the stack sum, no label-to-label overlap, wholesale suppression at 144 buckets and in a narrow window, and the pill-dodges-the-label case (which asserts the collision actually occurs, so the test can't silently stop exercising it). Full suite 246/246. No schema change.
