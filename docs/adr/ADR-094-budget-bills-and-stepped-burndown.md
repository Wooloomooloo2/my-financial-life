# ADR-094 — Budget bills (scheduled-backed lines) and the stepped burn-down

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-058 (budget redesign — `budget_line` / `budget_allocation`, `compute_matrix`, `compute_burndown`), ADR-023 (scheduled transactions), ADR-024/025 (superseded budget — first burn-down + the "stepped burn-down ideal" backlog item), ADR-026 (paintEvent charts). Owner-requested for the 1.0 ship.

---

## Context

Two related gaps in the budget surface:

**1. Bills aren't modelled.** A budget envelope today is a flat per-month allocation with a `role` (bills / saving / discretionary) that's only a label — it carries no date, no recurrence, and no link to the schedules arc (ADR-023). So a fixed obligation that lands once on a known day (rent on the 1st, a £25 weekly standing order) is indistinguishable from elastic discretionary spend, and the burn-down's **projection** treats both the same: it extends the *observed average daily rate* to month-end (ADR-058 R3, principle 12). For a discretionary envelope that's right; for a bill it's wrong twice over — before the bill is paid the projection under-counts it (no run-rate has accrued yet), and the moment it's paid the projection keeps *climbing* off the run-rate as if more of the same is coming, when in fact the obligation is **done for the period**.

**2. The burn-down looks cheap.** Three thin diagonal dashed lines (actual / ideal / projected) read as "out of place and somewhat cheap" (owner). The long-standing "stepped burn-down ideal" backlog item — the ideal/projection should *step down at known bill dates* rather than slope linearly — was never built because bills weren't modelled. Fixing (1) unlocks it.

The owner wants a **"This is a bill"** affordance on a budget item, with: a set date that appears on the Schedules screen; schedules and the budget kept in sync; the projection **flattening once a bill is paid**; and weekly / twice-monthly bills reflected as the right number of occurrences in a monthly view.

---

## Decision

A **bill is a budget line backed by a real `scheduled_txn`** (ADR-023) — not a new parallel concept. The schedule owns the date, cadence, amount, and paying account (which a category-level budget line otherwise lacks); the budget line links to it.

### Schema (migration 0030)

`budget_line` gains one nullable column:

```
scheduled_txn_id INTEGER REFERENCES scheduled_txn(id) ON DELETE SET NULL
```

`scheduled_txn_id IS NOT NULL` ⇒ the line is a bill. `ON DELETE SET NULL` means deleting the schedule quietly demotes the line back to a normal envelope rather than cascading it away. No other schema change; allocations, rollover, and roles are unchanged.

### Flows (owner forks via `AskUserQuestion`)

- **Budget setup → pull in schedules.** The primary path: a *"Add scheduled transactions to this budget"* step lists the file's active expense/transfer schedules; the user ticks the ones to include; each becomes a **bill line** (category from the schedule, allocation seeded from the schedule's estimated amount × occurrences-per-month, **fields adjustable**), linked to that schedule.
- **New bill from a line → the full Schedule dialog.** Marking a not-yet-scheduled line as a bill opens the existing `ScheduleDialog` (seeded from the line: category, suggested amount), so the user sets the **paying account**, cadence, and date there — reusing the familiar screen rather than a cut-down inline form.
- **Schedule → budget, ask each time.** When a schedule is created on a category the default budget doesn't yet cover, the app **asks** "Add this to your budget?" (rather than silently adding or ignoring) — the reverse-sync the owner asked for, gated against budget clutter.

### Paid detection — amount-match (owner fork)

A bill's projection stops growing once it's paid, detected by **matching actuals against the scheduled estimate** (not a cruder "due date passed = paid"). At the line level this is exact because the line's category *is* the bill's category, so the line's actual outflow is the bill's payments:

```
unpaid(line) = max(0, bills_total_month(line) − actual_to_date(line))
```

Occurrences are matched greedily in due-date order; an occurrence is paid while the running actual covers its estimate. The **unpaid** remainder is what still projects.

### Bill-aware, stepped `compute_burndown` (pure)

`compute_burndown` gains an optional `bill_occurrences: list[BillOccurrence]` (each `(category_id, day, amount)`, expanded by the new pure `bill_occurrences_in_month(...)` from the in-scope linked schedules). With bills present:

- **actual** — cumulative total outflow through today (unchanged; rendered as a **step** function).
- **ideal** — a **staircase**: each planned bill is a vertical step at its due day, and the *discretionary* remainder (`max(0, total_planned − bills_total)`) is spread linearly across the month. `ideal[d] = Σ(bill amounts due ≤ d) + disc_planned · d/period_days`.
- **proj** — `actual_to_date` + the **unpaid** bill occurrences as steps at their due days (an overdue-but-unpaid bill steps at *today*) + the discretionary run-rate applied to **non-bill** spend only (`disc_rate = discretionary_actual_to_date / today_day`). So a paid bill drops out of the projection (its `unpaid` is 0) and the line goes flat; an unpaid bill puts a step where it's expected; and weekly/twice-monthly bills contribute one step per in-month occurrence.

With no bills in scope the function is byte-for-byte the old behaviour (discretionary = all spend, linear ideal, run-rate projection) — full backward compatibility.

### Chart (2b — the stepped staircase)

`burn_down_chart.py` is rebuilt to draw **step functions** (hold-then-jump) with a **filled actual area**, replacing the three thin diagonal dashed lines. Actual is the solid filled staircase; ideal and projection are lighter stepped guides; the Budget reference + Today marker stay. paintEvent, per the chart-engine preference (ADR-026) — no new dependency.

---

## Consequences

- A fixed obligation is now first-class: it shows on the Schedules screen, posts/auto-posts through the existing machinery (ADR-023), and the budget projection treats it as a one-(or-N)-time event that *stops* once paid — the behaviour the owner called out.
- The burn-down reads as an intentional, ledger-honest staircase instead of cheap diagonals; the stepped-ideal backlog item is closed.
- `BudgetLine` gains `scheduled_txn_id`; `add_budget_line` / `update_budget_line` carry it; new repo helpers link/unlink a bill, list schedules-not-in-the-budget (setup picker), and gather a budget's linked bill schedules. `compute_burndown` gains an optional param (default-compatible).
- **Deferred / boundaries:** one schedule per bill line (a category with several bills links the representative one; the others still post — a multi-bill-per-line model is a follow-up); the loan arc (ADR to come) will reuse this exact bill machinery for the auto-budgeted principal/interest split; coupon-income scheduling from ADR-093 bonds slots onto the same schedule↔line link.

### Rejected alternatives

- **A date-only `is_bill` flag on the budget line (no real schedule).** Lighter, but it wouldn't appear on the Schedules screen and couldn't post/auto-post — contradicts "appears on the schedule," and would duplicate the recurrence math the schedules arc already owns.
- **"Due date passed = paid" projection.** Simpler (no matching), but mis-handles a bill paid early or late; the owner chose amount-match for accuracy, which is exact at the line level anyway.
- **Silent schedule→budget auto-add.** The owner's literal "added if not there" — but it risks turning every subscription into a budget envelope unbidden; gated behind an ask.
- **A separate `bill` table.** The obligation already has a home (`scheduled_txn`); a side table would duplicate it and split the recurrence logic.
- **Keeping the diagonal lines, just restyled.** Smallest change, but the owner explicitly wanted a different shape, and the staircase falls out of the bill model for free.
