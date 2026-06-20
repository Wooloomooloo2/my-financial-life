# ADR-087 — Home budget card measures spend against envelope `available`, not bare `allocation`

**Date:** 2026-06-20
**Status:** Accepted.
**Amends:** ADR-075 (home dashboard). **Builds on:** ADR-058 (budget redesign — envelope/zero-sum planning).

---

## Context

The home dashboard's budget card and the Budget window disagreed: for June 2026 the home
card read **"£2,722.45 of £1,900.00 spent — Over by £822.45"** while the Budget window's
"Expenses — total" read **2,722.45 / 19,673.42** — comfortably under. Same `spent`, two
different denominators.

Both views derive from the *same* `bc.compute_matrix` output. The divergence was purely in
which subtotal field each one read:

- **Budget window** (`budget_monthly_view.py:344,395`) measures spend against
  **`cell.available`** = `allocation + carry_in` — this month's fresh allocation **plus the
  carried-over envelope balance**. This is the envelope/zero-sum model of ADR-058: you spend
  against what's *in the envelope*, which includes prior-month rollover.
- **Home card** (`home_dashboard.py`) read **`cell.allocation`** — only this month's fresh
  assignment, discarding all rollover. That sum equals the budget header's
  *"Assigned (Jun 26): 1,900.00"*.

Worked example (June 2026 expense envelopes):

| Envelope | allocation | + carry_in (rollover) | = available |
|---|---|---|---|
| Bills | 800.00 | +70.99 | 870.99 |
| Food | 1,100.00 | +894.19 | 1,994.19 |
| Household | 0.00 | +8,485.22 | 8,485.22 |
| Personal Spending | 0.00 | +3,562.56 | 3,562.56 |
| Travel & Vacation | 0.00 | +4,760.46 | 4,760.46 |
| **Total** | **1,900.00** | | **19,673.42** |

So the card compared £2,722.45 against £1,900.00 (allocation) and cried "over"; the Budget
window compared it against £19,673.42 (available) and reported "under". Neither was an
arithmetic error — but the card answered the *wrong question* for a rollover budget, and the
two screens contradicting each other on the same month is a correctness/trust bug.

## Decision

The home budget card measures spend against **`available`**, matching the Budget window and
the ADR-058 envelope model.

`home_dashboard._budget_card` now sets `BudgetCard.planned = cell.available` (was
`cell.allocation`). The `BudgetCard.planned` field is documented as carrying envelope
*available* (allocation + rollover). No change to `home_view.py`: it already renders
`spent / planned`, the percentage bar, and the "Over by" note off `planned` — they now read
correctly. No change to `budget_calc.py`.

## Consequences

- The home card and the Budget window now agree for any month: a budget is "over" on the home
  page **iff** the Budget window's expense subtotal bar is over. The June case now shows
  *under* budget.
- The card reflects true envelope health: a month with heavy rollover (e.g. Household's
  £8,485 carried in) will not flag "over" merely because this month's fresh top-up was small.
- Trade-off: a deliberately over-spent envelope that is only rescued by rollover will read as
  "fine" on the card — which is exactly the envelope model's intent (the rollover *is* the
  cushion). Per-envelope over-spend remains visible in the Budget window's individual bars.
- No data migration; pure presentation fix.
