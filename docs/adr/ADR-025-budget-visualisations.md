# ADR-025 — Budget visualisations (round C — burn-down + summary bar + cadence subtitles + scheduled projection)

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-018 (Reports framework — QtCharts, no-pies rule); ADR-019 (Net Worth — `ProportionalBar` reused here); ADR-020 (Account transfers — inform the cadence-period actuals bucketing); ADR-023 (Scheduled transactions — feeds the projection); ADR-024 (Budget core — round B; this ADR closes its deferrals). Third and final round of the budget arc.

---

## Context

ADR-024 deferred four visualisation items to a follow-up round so the budget core could ship in usable shape first:

1. The **full-cadence-period subtitle** on non-monthly cards (a £1,800/year Holidays budget showing the pro-rated £147/month figure but also "this year: £450 of £1,800" so the long view is preserved).
2. A **burn-down chart** answering "am I on pace?" with cumulative actual outflow against a linear ideal pacing line.
3. A **summary visualisation** — Simplifi showed a donut, but ADR-018 enshrined a no-pies rule, so a horizontal proportional bar.
4. The **scheduled-but-not-posted projection** — schedules due in the screen period that the user hasn't materialised yet, surfaced on each card as a forward-looking "+£X expected" badge.

A bug surfaced after ADR-024 shipped (a transfer-kind budget category was still subtracting from the Income tile via `planned_bills` / `planned_saving` / `planned_spending` even when both halves of the transfer were inside the perimeter and the actuals correctly cancelled). The ADR-024 amendment fixed the tile-math leak — that fix is independent of this round and is recorded inline in ADR-024.

This ADR is scoped to the four visualisation items plus the data-layer additions they need. Reports-menu cross-period analytics (Budget vs Actual time series) and the per-card spend-history sparkline stay deferred to a future polish pass.

## Options considered

### Where the cadence-period actuals are computed — repository / budget_calc / UI window (chosen: UI window, fed into budget_calc)

The full-cadence-period subtitle needs per-card actuals over the calendar period containing today at the card's native cadence. Three places that math could live:

- *In the Repository*: a new method `compute_cadence_period_actuals_for_budget(budget_id)` doing N SQL queries inside the Repository. Tightly couples Repository to bucketing logic (which is currently the budget_calc module's job).
- *In `budget_calc.compute_budget_view`*: have it take the Repository as a dependency and run the queries internally. Breaks the pure-function contract that made round-B's smoke tests trivial.
- **In the UI window, fed back into `compute_budget_view` as optional inputs** (chosen): the BudgetWindow runs one perimeter-txn query per unique non-monthly cadence (≤4 queries), buckets in Python via `nearest_budgeted_ancestor`, and hands the result to `compute_budget_view` as `cadence_period_actuals_by_category`. `compute_budget_view` stays a pure function; the per-cadence query count is bounded by the small cadence enum, not by category count.
  - Same shape used for the scheduled-due-per-card map: window queries `list_perimeter_schedules_due_through(budget_id, period_end)`, filters to schedules whose `next_due_date >= period_start` (to exclude overdue from prior periods), buckets, and passes the result in.

### Cadence period for the "this <unit>" subtitle — global anchor / per-card anchor (chosen: global)

- *Per-card anchor*: each card carries its own anchor date so a Friday-paid biweekly schedule shows a Friday-anchored fortnight. Matches `scheduled_txn`'s anchor model. Doubles the per-card UI surface and isn't strictly necessary — the subtitle is an informational aid, not a per-pence reconciliation tool.
- **Global anchor rule** (chosen, matching ADR-023): weeks start Monday (`d - timedelta(d.weekday())`); quarters and years are calendar. Bi-weekly is genuinely ambiguous without a per-schedule anchor — v1 treats it as the 14-day window *ending on the reference date* so the subtitle answers the natural "what's hit the budget in the last fortnight" question. Per-schedule anchors are a future refinement.

### Burn-down ideal line — straight line over total planned / per-category pacing / spending velocity (chosen: straight line over total planned)

- *Per-category pacing*: each card has its own ideal line and the chart sums them. Honest but visually noisy; the user already has the cards for per-category detail.
- *Spending velocity (rolling average from prior periods)*: predicts what they're likely to spend rather than what they planned. Useful but conflates "are you on plan?" with "are you on trend?" — two different questions.
- **Linear ideal across the period** (chosen): at day `d` of `N`, `ideal(d) = total_planned × d / N`. Total planned outflow = `bills + saving + planned_spending` (income excluded — it's a depletion chart, not a net-cash one; transfers already excluded by ADR-024's tile fix). The actual cumulative line crosses or stays under the ideal: a single glance answers "am I burning faster than the plan allows?". A vertical dotted line marks today so the user can read where they are in the period.

### Summary visualisation — donut / horizontal proportional bar / stacked-bar mini chart (chosen: horizontal bar)

The Simplifi screenshot has a donut. ADR-018 set the no-pies rule for the Spending Over Time report — the reasoning (slice-size comparison is unreliable) applies just as much here.

- *Donut*: matches Simplifi but contradicts the standing rule. Rejected.
- *Stacked bar mini-chart*: needs a chart axis for the trivial case of "five numbers summing to a fixed total" — overkill.
- **`ProportionalBar` reused from the Net Worth report** (chosen, ADR-019): one horizontal segmented bar showing Bills / Saving / Planned spending / Other spending / Available as positive segments summing to Planned income (modulo rounding). A small colour-keyed legend underneath gives exact figures. Zero-amount segments are skipped (already part of the widget's contract) so an empty income tile produces a graceful empty bar.

### Burn-down chart library — QtCharts / pyqtgraph / matplotlib (chosen: QtCharts)

- *pyqtgraph*: faster and more interactive, but a new dependency.
- *matplotlib*: heavyweight, doesn't sit cleanly inside a Qt window.
- **QtCharts** (chosen, ADR-018): already on the PySide6 wheel, already used by the Spending Over Time report. Two line series, a value axis, and a vertical "today" marker implemented as a two-point series so it stays in the chart's coordinate system. No extra dependency, idiom-consistent with the existing report.

### Scheduled projection scope — overdue + due / due in period only (chosen: due in period only)

A schedule with `next_due_date = 2026-04-12` is technically "still due" if the user never posted it. Two cuts:

- *Include overdue*: surface every outstanding schedule on the budget — the user sees "you have £620 of unmet obligations sitting in your schedule list". Honest but noisy, and conflates "this month's plan" with "you have housekeeping in the Schedules dialog".
- **Due in this screen period only** (chosen): the projection shows only schedules whose `next_due_date` is between `period_start` and `period_end`. Overdue schedules from prior periods belong in the Schedules dialog where the user can post them or fix the next-due date; the budget screen stays focused on "what's planned and still to come this month". A future polish item could add an "X overdue schedules" badge somewhere on the budget window, but it doesn't belong in the per-card numbers.

### Card subtitle — single line / two lines (chosen: two lines, second only for non-monthly cadences)

- *Single richer line*: cram the cadence-period figure into one line; risks running off the card width on narrower windows.
- **Two lines, second is non-monthly-only** (chosen): monthly cards stay single-line (subtitle = "£X spent of £Y · N txns" — same as round B). Non-monthly cards get a second italic line: "£1,800 annually · this year: £450 of £1,800". Distinct visual treatment makes it scannable.
- The scheduled-projection badge ("+£X expected") slots into the first line so monthly cards still get the projection without growing a second line just for it.

## Decision

### Data layer — no schema change

Everything builds on the existing `Repository.list_perimeter_txns(budget_id, start, end)` and `Repository.list_perimeter_schedules_due_through(budget_id, through_date)` methods from rounds A and B. No new repository methods, no migration.

### Computation — `mfl_desktop/budget_calc.py` additions

- **`cadence_period_containing(cadence, ref_date) -> (start, end, label)`** — calendar period at the cadence containing the reference date. Weekly is Monday-Sunday; biweekly is the 14-day window ending on `ref_date`; monthly/quarterly/annual are calendar.
- **`compute_burn_down(perimeter_txns, summary, period_start, period_end, today=None) -> BurnDownData`** — single O(n) pass over perimeter txns, returns x_days + actual_cum + ideal_cum + today_day for the chart.
- **`compute_summary_breakdown(summary) -> SummaryBreakdown`** — trivial reshape of the summary tiles into the five positive segments for the proportional bar.
- **`compute_budget_view`** grows three optional kwargs (`cadence_period_actuals_by_category`, `cadence_period_label_by_category`, `scheduled_due_by_category`) populating new fields on each `BudgetCardData`. Defaults are empty dicts → identical to round-B behaviour.
- **`BudgetCardData`** gains `cadence_period_actual`, `cadence_period_label`, `scheduled_due_in_period`.
- New dataclasses **`BurnDownData`** and **`SummaryBreakdown`**.

### UI — `mfl_desktop/ui/burn_down_chart.py` (new)

`QChartView`-backed widget, fixed height 220-260px, two `QLineSeries` (Actual red solid, Ideal grey dashed) plus a today-marker dotted vertical line implemented as a two-point series. Legend at bottom. Calling `set_data(BurnDownData)` clears and redraws.

### UI — `mfl_desktop/ui/budget_window.py` additions

- Two new methods on the window:
  - **`_compute_cadence_period_actuals`** — for each unique non-monthly cadence among budgeted categories, one perimeter-txn query, bucket via `nearest_budgeted_ancestor`. ≤4 extra queries per refresh.
  - **`_compute_scheduled_due_per_card`** — one `list_perimeter_schedules_due_through` query for the screen period, filter to schedules due in `[period_start, period_end]`, bucket, sum magnitudes of outflow estimates.
- `reload()` calls both, passes the resulting dicts into `compute_budget_view`, and feeds `summary` into `compute_burn_down` + `compute_summary_breakdown`.
- New widgets in the layout, between the four-tile strip and the cards scroll area:
  - **Proportional bar** with a colour-chip legend line (Bills / Saving / Planned / Other / Avail.).
  - **Burn-down chart**.
- **`_CategoryCard`** subtitle update:
  - First line stays "£X spent of £Y · N txns", with an "+£Z expected" suffix when `scheduled_due_in_period > 0`.
  - Non-monthly cards get a second italic line "£Y <cadence> · this <unit>: £A of £Y".

### Colours

Palette for the proportional bar segments (also used in the legend chips):

- Bills            `#c2410c`
- Saving           `#7c3aed`
- Planned spending `#2563eb`
- Other spending   `#6b7280`
- Available        `#16a34a`

The burn-down chart uses `darkRed` for Actual, `darkGray` for Ideal, `darkBlue` for the today marker (QtCharts defaults consistent with the Spending Over Time report's choices).

## Consequences

### Positive

- **The full-cadence-period subtitle closes the round-B promise** to the user that an annual or quarterly budget would still show its native-cadence progress alongside the pro-rated monthly view.
- **The burn-down chart turns the four scalar tiles into a temporal answer** — "am I on pace today?" is now visible without doing the (planned × day/N) math in your head.
- **The proportional bar matches the Simplifi mental model without violating the no-pies rule.** A horizontal segmented bar reads more cleanly for five-segment proportions than a donut anyway — slice angles are notoriously hard to compare.
- **Scheduled projection turns the budget from a backward-looking report into a forward-looking plan.** "£200 spent + £15.99 expected" tells the user the month isn't done. Doesn't double-count: the £15.99 isn't added to `period_actual`; it's a separate badge.
- **`compute_budget_view` stays a pure function** — the round-C inputs are optional kwargs that default to "round-B behaviour". The fixture-based testability that made the ADR-024 bug straightforward to fix carries forward.
- **No schema change, no migration, no new repo methods** — everything reuses primitives from rounds A and B.

### Negative / trade-offs

- **Cadence-period queries run on every screen refresh.** With ≤4 unique non-monthly cadences each producing one perimeter-txn query, that's a small bounded cost — at MFL's scale (~1,300 txns) it's microseconds per query. A larger ledger or a future per-schedule anchor model would tilt this differently.
- **The biweekly cadence period is "ending on today"**, which isn't the user's pay-fortnight if their biweekly anchor was Friday. Tolerable for the subtitle informational role; would need a per-schedule anchor on the budget_category row to fix properly.
- **The burn-down's ideal line assumes uniform daily pacing.** A user with a £200 mortgage on the 1st and £400 of everything else over the rest of the month would expect a step-then-line shape, not a single straight line. Linear-from-zero is intentionally simple; a "stepped ideal" that knows scheduled txn dates is a future polish item.
- **The scheduled-due badge only counts schedules due in the screen period.** Overdue schedules silently don't show on the budget screen — the user has to spot them in `Manage → Schedules`. An "X overdue" badge somewhere on the budget header is a likely follow-up.
- **The proportional bar segments sum to `planned_income`**, so a user who hasn't budgeted income sees an empty bar. The empty state isn't error-y (the widget gracefully skips zero segments) but it's also not informative. A "set up an income budget to see this filled in" hint inside the bar is a future polish.
- **Five visualisation elements stacked vertically eat ~480px before the cards.** At the default 760px window that leaves ~280px for cards, which is tight but workable. A vertical splitter between chart and cards is a future affordance.

### Ongoing responsibilities

- **`cadence_period_containing` defines the global anchor rule.** Any future per-schedule-anchor work needs to layer over this, not replace it — the rule is invoked by the budget screen, the schedule post path, and any future budget-cadence-aware code, and inconsistency between them would produce subtle off-by-one bugs.
- **The proportional bar palette is duplicated in `budget_window.py`** (segment colours + legend chip styles). If the palette is ever extracted into a colour module, both lists go together.
- **The burn-down ideal line is intentionally not piecewise.** If a future round wants stepped-ideal pacing driven by scheduled txn dates, it goes in `compute_burn_down` and the chart widget doesn't need to know.
- **The scheduled projection treats `next_due_date < period_start` as "overdue, not shown".** A future overdue surface (badge, dialog, report row) consumes the same Repository method with a different filter and should not re-define what "due" means.
