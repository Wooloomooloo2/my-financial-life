# ADR-136 — Home budget card shows this month's plan, not rollover-inflated available

**Date:** 2026-07-05
**Status:** Implemented
**Related:** ADR-058 (envelope / zero-sum budget, rollover). ADR-087 (the prior decision this supersedes — home card used `available`). ADR-075 (home dashboard).

## Context

Owner confusion: the home **Budget** card read "£729.04 of £14,410.20 spent", but the Budget page showed "£3,493.28 assigned" and a "Pool" of £7,210 — three numbers that look like they should agree and don't.

They measure three different things (all correct):

- **Pool** = the real balance (+ available credit) of the accounts the budget watches — the money on hand to allocate (`compute_perimeter_pool`, ADR-058 D2/R4a).
- **Assigned** = this month's non-income allocation — the monthly plan (`assigned_by_month`).
- The home card's **"of £14,410.20"** was the expense section's **`available` = this month's allocation + accumulated rollover carry**. Every expense envelope is `rollover='accumulate'` (ADR-058 D3), so months of underspend pile up; the denominator ballooned and read like a £14k *monthly* budget. (Confirmed on the dev file: the same card computed £0 spent of £18,981 available, of which £17,081 was pure carried-in rollover.)

ADR-087 deliberately switched the home card from `allocation` to `available` so it agreed with the Budget page's *per-envelope* bars. But at the whole-budget subtotal that sum-of-availables is dominated by accumulated rollover, so the headline misleads.

## Decision

The home card headline is a **monthly** figure again: **spent vs this month's expense allocation**, with the accumulated rollover surfaced *separately* as a "+£N rolled over available" line (owner's pick over relabelling the big number or leaving it).

- `BudgetCard` now carries `planned` = this month's expense **allocation** (`cell.allocation`, matching what the Budget page calls "assigned") and a new `rollover` = `cell.available − cell.allocation` (the carried-in cushion, floored at 0). `_budget_card` sets both from the expense subtotal cell.
- The renderer shows **"{spent} of {planned} budgeted this month"**, a progress bar of spend against this month's plan (over ⇒ "Over this month's plan by £X" in red), and a muted **"+{rollover} rolled over available"** sub-line when there's carry. When a month's plan is £0 (an envelope funded purely by rollover) the headline drops to "{spent} spent this month" and the bar falls back to the rollover cushion so it's still meaningful.

Rejected: keeping `available` with a clearer label (still a scary five-figure denominator); measuring spend against `allocation + rollover` (the exact ADR-087 ballooning); showing the Pool on the home card (it's the whole-budget cash anchor, not a per-month expense figure — it belongs on the Budget page, where it already is).

## Consequences

- The home card now reconciles intuitively with the Budget page: the denominator is a monthly number in the same ballpark as "assigned", and the rollover you've banked is shown but no longer inflates the headline.
- The per-envelope bars on the Budget page still use `available` (correct there — a single envelope's room *is* allocation + its own rollover); only the home summary changed.
- View + dashboard-DTO only; no schema change, no migration.
- Verified headless (offscreen, live `mfl_dev.mfl`): the card renders "£0.00 of £1,900.00 budgeted this month" + "+£17,081.58 rolled over available" (was "£0.00 of £18,981.58"); compiles clean.
