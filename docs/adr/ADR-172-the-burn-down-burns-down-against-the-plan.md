# ADR-172 — The burn-down burns down, against the plan

**Date:** 2026-07-17
**Status:** Implemented
**Related:** ADR-058 R3 (the monthly view and its burn-down; D3's rollover, whose `available` is the number this stops pacing against). ADR-094 (the bills staircase, kept). ADR-171 (the monthly row — which states the *ceiling*, freeing the chart to be about the *plan*; and the same ratchet-to-zero move). ADR-164 (a report leads with what it is — applied here to a chart). ADR-167 (the frozen-colour ratchet). ADR-026 (the hand-rolled paintEvent chart idiom). ADR-076 (the token layer).

## Context

Owner-reported: *"I've never been fully satisfied with this chart."*

That is an aesthetic complaint on its face, and it isn't one. Reproducing the screenshot's budget and measuring what the chart plots:

| | |
|---|---|
| Assigned — the month's plan | **£3,673.27** |
| The dashed **"Budget"** line | **£19,892.89** — 5.4× the plan |
| What **"Ideal"** claimed you should have spent by the 17th | **£10,909** |
| Share of the y-axis occupied by the actual data | **7%** |

`total_planned` was the scope's **`available`** — allocation *plus accumulated rollover*. Six months of underspending had piled into it. So the chart paced the reader against £19,892 in a month they had assigned £3,673 to, and the "Ideal" line told them they were nine thousand pounds *behind on spending*. That is not a plan anyone failed to meet; it is an artifact of the carry.

And because the budget line sets the y-axis, the actual spending was crushed into the bottom 7% of the canvas with 93% empty above it. **The chart was not badly drawn. It was correctly drawing a comparison that had no meaning.** The eye knew; the numbers said why.

Three more defects sat on top:

- **It was a burn-down that went up.** Every series climbed toward a ceiling. The module docstring said "depletion chart" and "spend depletion"; nothing depleted. The name, the docstring and the picture disagreed.
- **`projected_end` was computed and read by nothing.** Grepped: defined in `budget_calc`, returned in `BurnDownData`, and used by no view and no test. The chart worked out the exact answer to "will I come in under?" and then made the reader eyeball where a dashed line stopped.
- **Red meant nothing.** The actual line was a hard-coded `#dc2626` red-600 with a red fill, *always* — so a reader comfortably under budget got a chart shouting in alarm-red. Five frozen light-theme hexes, the whole of this module's ADR-167 allowance.

## Decision

**The chart burns down, and it paces against the plan.**

The owner settled the two questions that were genuinely theirs, and overruled the recommendation on the second — correctly.

**1. The pacing target is the month's `allocation`, never `available`.** A rollover surplus is a **buffer, not a target**: you do not aim to spend it, so it has no business setting the pace. Same data, pacing against the plan, takes the series from 13% to **72%** of the canvas — the chart suddenly has something to say. Symmetrically, a carried-in *deficit* does not lower the target either: the plan is the plan, and the debt is reported on the row where ADR-171 put it.

This works precisely *because* of ADR-171. The row now says `£1,460.00 of £19,892.89` — the ceiling, stated. That frees the chart from having to carry it, and the two surfaces divide the labour: **the row says what you have, the chart says whether you are pacing it.** The buffer still gets a word in the verdict caption (`+£16,219.62 rolled over if needed`) — it is the difference between "over budget" and "over budget with nothing behind it".

**2. It descends.** Start at the plan, fall as you spend, and **the day it reaches zero is the day you run out**. That reading — `Runs out · 16` — is the prize, and a rising line cannot give it: "you'll spend £4,100 of £3,673" is a subtraction the reader has to do; "you run out on the 16th" is a date. The axis extends *below* zero when spending has overshot, because an overspend is the one thing the chart exists to warn about and clamping at zero would hide it.

**3. The wedge is the reading.** Lifted from a burn-down the owner shared for reference: shade between Remaining and Plan — green where more is left than planned, red where less. The colour *is* the comparison, so **the legend is gone** (22px back, and one less decode step: three dashed greys and a swatch strip made the reader look away from the data to interpret it). The three series are direct-labelled at their own ends instead.

**4. Colour means over.** Remaining is plain ink — it is a fact, not a judgement. The **projection** carries the verdict: green when it lands above zero, red when it crosses. Red now appears if and only if something is wrong.

**5. The verdict is stated, in words.** `On track · £1,010.92 left on 31 Jul` / `Over budget already · £3,383.79 over by month end`. This is ADR-164's principle ("lead with what it is") applied to a chart, and it is what finally spends `projected_end`. It lives in the scope row rather than inside the paint, so it costs no chart height and rides the token layer like any other label.

`compute_burndown` keeps producing **cumulative spend** — that is what the bills staircase and the run-rate naturally build, and it stays the single representation. *Remaining* is a pure function of it (`total_planned − cumulative`), derived in the chart. Model and view, not two models. What did move into the model is the arithmetic a test should be able to reach without a Qt event loop: `runs_out_day` and `projected_remaining`.

## Rejected

- **The endpoint callout pills from the reference chart** ("End of Dec 31 · £5,546.44 GBP left"). Genuinely good, and right for *that* chart — which has no headline, so the pills are how it states its numbers. We have a headline slot in the scope row for free, and a worded verdict beats a bare number pill. Two places both saying "£1,011" is exactly the redundancy ADR-171 just stripped out of the rows.
- **Keeping the burn-*up* and only rescaling it.** My recommendation, and the owner overruled it. Rescaling fixes the squash but not the name, and it forfeits the run-out date — which turns out to be the most actionable thing on the chart. The pace-vs-name mismatch was real and worth paying to fix.
- **Clamping the axis at zero.** Would hide the overspend. See above.
- **Pacing against `available` but rescaling so the data isn't crushed.** The alternative the owner was offered. Coherent — if you think of rollover as this month's spending money, the old chart was right in principle. It isn't the owner's model, and it produces a chart that says "you are £9,000 behind on spending" to someone who is fine.
- **Suppressing the rollover buffer from the UI entirely** now that it no longer paces. It is real money and the reader should know it is there; it just isn't the target. Hence the caption.
- **Reading the buffer from `cell.carry_in`.** The obvious field, and wrong here: a section subtotal sums its rows' `available` but hard-codes its own `carry_in` to zero (`compute_matrix` step 4), so `carry_in` finds nothing on the **whole-budget scope** — where the buffer is largest and most worth saying. `available − allocation` is the definition anyway.
- **A literal `#ffffff` for the Today pill's text.** It *is* white in both themes, so ADR-167 would have excused it. But a hex is indistinguishable from the frozen light-theme ones the ratchet hunts, and `on_accent` already exists and is white in both. Zero pixels change; the token says out loud that the choice was deliberate.

## Consequences

- **The chart answers its question in the first line now**, and the picture agrees with its name. The screenshot's shape — a flat red line hugging zero under 93% of empty canvas — is gone, because the comparison that produced it is gone.
- **`projected_end` is finally spent**, three ADRs after it was computed.
- **`burn_down_chart.py` is at zero frozen colours**, ratchet tightened 5 → 0. ADR-167's staleness check demanded it the moment the module got cleaner — the second time in two ADRs that it has caught this unprompted (ADR-171 took the monthly view 3 → 0).
- **Rollover budgets change most; non-rollover budgets not at all.** With no rollover, `available == allocation` and the pacing target is identical to before — the inversion and the verdict are the only visible change.
- The **carried-in-deficit** case is a deliberate asymmetry to keep in mind: the chart paces against the plan and will say "on track" while the row says you are £30 in the hole from last month. That is the division of labour working as designed, but it is a real seam, and if it bites, the answer is a second caption — not a second pacing rule.
- The wedge approximates its zero-crossings at the day boundary (one sample per day), which is sub-pixel at any realistic width. Not worth interpolating.

`tests/test_budget_burndown.py` 12/12. The calc half is Qt-free: comfortably-under never runs out; a projection crossing zero **names the day**; an already-blown plan reports the day it *happened* (from the actuals, not the forecast); spending exactly the plan counts as running out (`>=`, because remaining hits zero and there is nothing left); a **zero plan is not a false alarm** (every day would otherwise qualify); and `projected_remaining` reconciles with `projected_end`. The view half drives the real window: **the burn-down paces against the plan and not the rollover buffer** — built on the exact shape that caused the bug, `available` more than 4× `allocation` — the target ignores a carried-in deficit too, a missing cell is a zero rather than a crash, the verdict states the answer with a currency glyph, reddens only when over, and names the buffer without pacing on it.

Verified by **rendering four scenarios in both themes** — comfortably under, an overspend that runs out mid-month, a plan already blown before today, and nothing spent at all. That is how the `Runs out · 16` label was found being overprinted by the `Remaining` end-label's plate (fixed by drawing the marker last and standing `Remaining` down when a run-out is on screen — when you have run out, that is the more important of the two). As in ADR-171: the string assertions passed the whole time.

Full suite 393 passed, 1 failed — the same pre-existing, unrelated `test_drilldown_account_subset::test_split_row_double_click_opens_split_dialog` recorded by ADR-168/170/171, confirmed failing on the untouched tree. No schema change.
