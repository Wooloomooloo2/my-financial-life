# ADR-187 — Pending stragglers carry into the next statement

**Date:** 2026-08-02
**Status:** Implemented
**Related:** ADR-182 (opt-in "include pending within the statement dates" — this ADR amends its date bound). ADR-130 (the confidence ladder and status-driven reconciliation). ADR-040 (the statement-reconciliation flow; the any-date straggler rule this brings pending into line with).

## Context

Owner-reported: *"when reconciling a statement like 1st July to 31st July, if there are items earlier than this date which are unreconciled, they should always be pulled in to the next statement. Sometimes it's just timing differences that meant they were left out of the prior statement, but if we can't include them in the next statement, I have to artificially include them by extending the statement date backwards."*

Two of the three statuses already behave this way. `list_reconcilable_txns` makes **matched** eligible at any date and **cleared** eligible at any date behind the "Include cleared" toggle — deliberately, so an old straggler can still be caught (ADR-040). Only **pending** was fenced: ADR-182 made it eligible when `include_pending` *and* `COALESCE(bank_posted_date, posted_date) BETWEEN period`.

That `BETWEEN` is the bug. Re-reading ADR-182's own reasoning, the danger it was guarding against is named precisely: *"The danger ADR-130 guarded against was **future** pending (next month's not-yet-real spending) becoming reconcilable."* The **upper** bound carries that entire argument. The **lower** bound came along for free with `BETWEEN` and protects against nothing — a pending row dated *before* the statement start is the opposite of not-yet-real. It is a purchase entered a while ago that the bank was slow to post, which is exactly the row a later statement ought to sweep up.

The consequence is the workaround the owner describes: back-date the statement's start to drag the straggler in, which corrupts the period on a statement that will be kept as a record. Real instances exist in the live file — a `pending` Amazon Prime charge dated 22 June sitting in Capital One USA, four in MS Pension UK — none reachable from a July reconcile without faking the dates.

## Decision

**Drop the lower bound on pending eligibility: a pending row is eligible when `include_pending` is on and it is dated on or before the period's END date.**

- **Query (`list_reconcilable_txns`).** `BETWEEN ? AND ?` becomes `<= ?` against the period end. Everything else is untouched: no `period` still means no pending row is eligible whatever the flag (the ADR-130 default), the flag is still opt-in and off by default, and matched/cleared gating is unchanged. Pending is now bounded *above only* — the one bound that was ever load-bearing.
- **Offered, never auto-ticked.** The auto-select preset keeps its `start_iso <= posted_date <= end_iso` guard, so a straggler appears in the list deselected, exactly like an old matched/cleared straggler. Ticking a months-old pending row must be a deliberate act, not something a preset does quietly while the owner reads the totals.
- **The warning counts what the gate hides.** `count_pending_in_period(account_id, date_from, date_to)` becomes `count_pending_reconcilable(account_id, date_to)`, ranging on or before the end date so it names *exactly* the rows ticking the box would surface. Reworded from "N pending transactions in this period are not shown" to "N pending transactions dated on or before `<end date>` are not shown". A warning that under-counts what the toggle would reveal is worse than no warning — it is the discovery path (ADR-182), so it has to be honest about the size of the backlog.

## Rejected

- **Auto-ticking stragglers as well.** "Always be pulled in" is about *availability*, not about presuming. A pending row from months ago may be a mistake, an abandoned entry, or a genuine straggler, and only the owner can tell. Matched/cleared stragglers are offered-not-ticked for the same reason; diverging here would be surprising.
- **A separate "Include earlier pending" toggle.** A second checkbox for a distinction that carries no risk. The upper bound already provides the safety; splitting the control would make the safe case harder to reach without making the unsafe case any safer.
- **A lookback window (e.g. pending within 60 days before the start).** An arbitrary number that would strand exactly the long-tail cases that motivate the change, and one more constant to explain. Matched/cleared have no lookback either.
- **Leaving the warning counting in-period.** Cheaper, but it would report a number the toggle then exceeds — the user ticks the box expecting 2 rows and gets 6. The count and the gate must be the same predicate.
- **Making pending unconditionally any-date, like matched.** That re-opens the June bug (future pending becoming reconcilable), which is the one thing ADR-182 got right and must not be lost.

## Consequences

- **The back-dating workaround is gone.** A timing difference that missed last month's statement is offered on this month's with its true dates, so the statement record keeps the period the bank actually used.
- **The ADR-182 safety property is intact and now stated more precisely:** future pending is un-reconcilable. Verified both ways offscreen on a copy of the live file — a July reconcile of Capital One USA surfaces the 22 June pending Amazon Prime charge with the toggle on and nothing with it off, while a *May* period with the toggle on still excludes that same June row.
- **A hand-kept account sees its whole backlog**, not just the current month's slice, the first time it ticks the box. That is the honest picture; the count in the warning now says so up front.
- **`count_pending_in_period` is renamed** to `count_pending_reconcilable` with a narrower signature (no `date_from`). One caller, one test. `count_cleared_in_period` is untouched — cleared has no date bound to mirror.
- No schema change; the ladder, `bank_posted_date`, and the tick/close paths are reused as-is.

`tests/test_reconcile_confidence.py` goes 9 → 10: the old `test_include_pending_is_date_bounded` becomes `test_include_pending_excludes_future_but_not_stragglers` (asserts both directions — an earlier row is offered, a later one is not), a new `test_include_pending_straggler_still_needs_the_flag` pins that the opt-in is still required, and the count test becomes `test_count_pending_reconcilable_mirrors_the_gate`. Full suite 461 passed, 0 failed — run file-by-file because `tests/test_budget_drilldown_editable.py` triggers a pre-existing Qt garbage-collection access violation at interpreter shutdown, reproduced unchanged on clean `master`.
