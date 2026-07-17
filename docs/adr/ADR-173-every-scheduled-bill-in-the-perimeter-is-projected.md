# ADR-173 — Every scheduled bill in the perimeter is projected

**Date:** 2026-07-17
**Status:** Implemented
**Related:** ADR-094 (bills, the linked-schedule mechanism, and the staircase projection this fixes the *source* of). ADR-172 (the burn-down redesign — this is the first thing looked at once the chart was readable enough to notice what was missing). ADR-170 (the subtree scope rule, whose last straggler this closes). ADR-024 (nearest-budgeted-ancestor bucketing — the rule the occurrences now obey).

## Context

Owner-reported, immediately after ADR-172 landed:

> It does not seem to take into account scheduled bills in the 'projected' — any scheduled transaction should be shown in the projected around the date it's due to post.

ADR-094 built exactly that: bill occurrences expanded from a schedule's cadence, stepping the projection at each due day, amount-matched against actuals so a paid bill drops out. The machinery was all there and correct. The **source** was wrong:

```sql
FROM budget_line bl
JOIN scheduled_txn s ON s.id = bl.scheduled_txn_id
```

`list_bill_schedules_for_budget` joined through **`budget_line.scheduled_txn_id`** — the link created by ADR-094's right-click "Make this a bill…". So a schedule was projected only if someone had separately gone to a budget line and linked it. A scheduled transaction created the ordinary way, in Manage ▸ Schedules, was invisible to the chart forever.

`mfl_dev.mfl` is the proof, and it is stark:

| | |
|---|---|
| Active scheduled transactions | **5** |
| Linked to a budget line | **0** |
| Seen by the burn-down | **0** |

Four of those five are real monthly subscriptions (Apple £36.95, Amazon Prime £9.99, STV £3.99, Disney+ £10.99) on perimeter accounts, categorised under a budgeted `Bills`. Every one of them was a known, dated, future outflow that the projection ignored.

**Why this survived ADR-094 and ADR-172:** `mfl_public.mfl` — the file the feature was demoed and eyeballed against — has 2 schedules and *both are linked*, because its demo builder links them. The sample data hid the bug. The one real file had 5 and none.

## Decision

**Membership is the perimeter, not the link.**

A schedule spends from this budget exactly when **its account is in the budget's perimeter** — which is the same rule `list_perimeter_txns` applies to the actuals that schedule will eventually become. `list_perimeter_schedules` replaces `list_bill_schedules_for_budget`, joining `budget_account` instead of `budget_line`.

The link (`scheduled_txn_id`) keeps its ADR-094 job — it is what makes an envelope a *bill* in the row UI, drives the "Make this a bill…" / "Remove bill" menu, and marks the line. It simply has no business gating the projection. Whether a future outflow is known does not depend on whether anyone filed it under an envelope.

This is **strictly better, not just broader**. A linked line whose schedule pays from an account *outside* the perimeter used to be projected — and its spending never lands in the perimeter's actuals, so the projection was inventing an outflow that budget would never see. The perimeter rule drops it correctly. (`mfl_dev` has one: a £450/month schedule on a "Polestar 2" account outside the budget.)

Three rules fall out of the source change:

**1. Occurrences bucket like actuals do.** `compute_burndown` now rewrites each occurrence's `category_id` to its **nearest budgeted ancestor** (ADR-024), the same mapping the actuals get. This matters the moment schedules stop being sourced from budget lines: ADR-094 read `bl.category_id`, which was *by construction* a budgeted category, so bucketing was a no-op it never needed. A schedule read straight from the perimeter carries its **own** category — `Cable and Internet`, under a budgeted `Bills`. Without bucketing the paid transaction lands on Bills, the occurrence sits on Cable, the amount-match never sees them as the same thing, and **the bill is counted twice**: once as actual, once as an unpaid projection. `BillOccurrence.category_id` becomes `Optional[int]`, because `None` — no budgeted ancestor at all — is a real bucket that the whole-budget scope must still project.

**2. Expense-kind only.** The chart plots expense outflows and nothing else (its whole-budget scope already discards income actuals), so projecting an income schedule would put a step in a line that will never move to meet it.

**3. The scope filter is a subtree test.** `o.category_id == target_category_id` becomes `is_ancestor_or_self(...)`, matching the txn filter one function down. **This is an ADR-170 straggler I missed**: that ADR made a group's burn-down plot against the group's whole roll-up and converted the *transaction* filter to a subtree test, but left the *bill* filter as an exact match — so a group scope showed its children's spending against its children's bills, minus the bills. Same class of bug, one line over, and it took a second look at the same function to see it.

## Rejected

- **Keeping the link as the source and telling the user to link their schedules.** Defensible as a feature ("bills are opt-in") and wrong as a default: the projection's job is to say what is going to be spent, and a schedule is going to be spent regardless of its paperwork. Opt-in would also make the chart quietly wrong rather than visibly empty, which is worse.
- **Unioning the linked list with the perimeter list.** Would double-count every linked schedule (a linked schedule on a perimeter account is in both), and the perimeter set already subsumes the linked one for every schedule whose spending this budget can actually see.
- **Including transfers.** `list_schedules_not_in_budget` uses `('expense', 'transfer')` for the bills *picker*, so there is a superficial consistency argument. But intra-perimeter transfers cancel (ADR-024) and the burn-down counts expense only — projecting a transfer would step a line that its own actual never touches.
- **Restricting to schedules whose category is budgeted.** Tidier bucketing (no `None` bucket), and it would silently drop real spending from the whole-budget projection — which counts *all* expense outflows, budgeted or not. The `None` bucket is the honest answer.
- **Keeping `list_bill_schedules_for_budget` around as dead code.** Nothing calls it now. Leaving a method that encodes the rule this ADR just established as *wrong* is a trap for the next reader, and the test asserts the unlinked condition directly (`all(ln.scheduled_txn_id is None ...)`) rather than through it. Deleted.

## Consequences

- **The projection now steps at every scheduled bill's due day**, which is what ADR-094 promised and what the owner asked for. On `mfl_dev`'s Bills scope: £13.98 at today (two subscriptions due on the 11th and 17th, unpaid, stepping at today per ADR-094's overdue rule), then £10.99 on the 29th and £36.95 on the 30th.
- **A budget's projection can now change without anyone touching the budget** — adding a schedule in Manage ▸ Schedules moves the chart. That is the point, and it is a behaviour change worth knowing about.
- The `None` bucket lumps *all* unbudgeted expense actuals together for amount-matching, so a large unrelated unbudgeted purchase can "pay off" an unbudgeted bill and drop it from the projection. This is the same approximation ADR-094 already makes per-bucket (any `Bills` spend pays a `Bills` bill), just coarser. Noted, not fixed — it needs per-schedule matching, which is a bigger change than this warrants.
- Schedules have **no archive path** in the app yet — `archived_at` is read (`list_scheduled_txns(include_archived=…)`) and never written. The `archived_at IS NULL` clause mirrors the method it replaces and is tested by stamping the column directly, so it is correct in advance of the UI rather than untested until it exists.

`tests/test_budget_scheduled_bills.py` 9/9: an **unlinked schedule is projected** (the bug) and **steps on its due day**, not smeared as a run-rate; a schedule **outside the perimeter** is not; income and archived schedules stay out; a schedule **buckets like its actuals** so a paid bill is not counted twice; an unpaid bill is projected exactly once; a **group scope sees its children's bills** (the ADR-170 straggler); and an unbudgeted schedule still counts toward the whole budget.

Both headline guards were confirmed to fail against the old code: the unlinked-schedule test with the source pointed back at the linked-bills query, and the group-scope test with the filter reverted to `==`.

Rendering `mfl_dev`'s real budget also caught a collision ADR-172 had left: on a scope that has spent nothing, `Remaining` is pinned to the top of the chart — exactly where the `Today` pill sits — and printed straight through it. The pill's rect is now shared between the two painters, and a label that would hit it ducks below its line instead. Third time in three ADRs that rendering has found what the assertions could not.

Full suite 402 passed, 1 failed — the same pre-existing, unrelated `test_drilldown_account_subset::test_split_row_double_click_opens_split_dialog` recorded by ADR-168/170/171/172, confirmed failing on the untouched tree. No schema change — `list_perimeter_schedules` is a new query over existing tables.
