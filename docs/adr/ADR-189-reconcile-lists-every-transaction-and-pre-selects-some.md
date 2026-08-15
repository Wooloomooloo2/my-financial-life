# ADR-189 — Reconcile lists every transaction; "automatically select" only pre-selects

**Date:** 2026-08-15
**Status:** Implemented
**Related:** ADR-130 (the confidence ladder and status-driven reconciliation — this ADR reverses its candidate *gating* while keeping its safety property). ADR-182 (which extended that gating to pending-within-the-dates; its date bound survives here, moved onto pre-selection). **ADR-187 (superseded** — it relaxed the same gate's *lower* bound so pending stragglers could be caught; this ADR removes the gate outright, which subsumes that fix and drops the toggle it still required). ADR-040 (the statement-reconciliation flow and wizard).

## Context

Owner-reported, with two screenshots. Page 1 of the wizard for a Capital One card: `Automatically select: Nothing`, both "Include…" boxes clear, a change in balance of £1,244.42. Page 2: **an empty table**, `WITHDRAWALS £0.00`, `DEPOSITS £0.00`, a red `MISSING £1,244.42`, and a banner reading *"10 pending transactions in this period are not shown — you entered them but the bank hasn't confirmed them."*

The account is kept by hand, so every row is `pending`. The wizard knew about all ten, had counted them, and had written a sentence explaining that it would not show them. There was nothing to check off.

The owner's statement of the rule is the whole decision: *"the 'automatically include…' function should simply be whether the transaction is pre-selected for the reconciliation calculation. It should NOT exclude the transaction from being shown in the reconciliation window for selection."*

**Why the design got here.** ADR-130 fixed a real bug — the June variance, where not-yet-at-the-bank 30-June card purchases were ticked onto a statement — by gating the *candidate set*: `matched` always eligible, `cleared` behind a toggle, `pending` never. ADR-182 then found that this stranded hand-kept accounts entirely, and relaxed it to pending-inside-the-statement-dates behind a second toggle, plus the warning banner as a discovery path.

Each step was locally reasonable, and the compound result is a reconcile screen that can show nothing at all. The banner is the tell: ADR-182 shipped it *because* the screen would otherwise be empty with no hint the option existed. A feature that needs a paragraph of explanation for why the list is blank is answering the wrong question.

**The conflation.** "Should this row be ticked for you?" and "should this row be on screen?" are different questions, and the confidence ladder is only a good answer to the first. Pre-ticking a row the bank never confirmed is a genuine hazard — the user trusts the ticks and the statement ties out against transactions that may not exist. *Listing* that row is not a hazard at all: it sits there unticked, next to the paper statement, and the user is the one who decides. ADR-130 reached for visibility because visibility was the lever nearest to hand, not because hiding was the property it needed.

## Decision

**Split the two apart. Listing is unconditional; the "Automatically select" controls govern ticks only.**

- **Query (`list_reconcilable_txns`).** Drops `include_cleared`, `include_pending` and `period`. One rule remains: a row is a candidate when it is **not already reconciled onto another statement** — plus the rows ticked into `include_statement_id`, so a resumed or viewed statement keeps its own ticks. No status test, no date test.
- **Pre-selection (`_preselect_statuses`) now carries ADR-130's safety.** The combo picks the base: `Matched Transactions` pre-ticks `matched`; `Nothing` pre-ticks nothing. The two checkboxes extend it down the ladder to `cleared` and `pending`. **The statement-period date bound survives, on the ticks** — ADR-182's central argument was that pre-selecting future pending is what's dangerous, and that is exactly as true here.
- **The checkboxes are relabelled to say what they now do** — "Also pre-select cleared/pending transactions…" — with a line under them: *"Every unreconciled transaction is listed for ticking either way — these choices only decide which ones start ticked."* Toggling one on the check-off page applies or withdraws that pre-selection **in place**, scoped to its own status class inside the dates, so it never disturbs a tick the user placed by hand and unticking is an exact undo of ticking.
- **Both boxes are disabled under `Nothing`**, which they extend and which pre-ticks nothing.
- **Both warning banners are deleted**, along with `count_cleared_in_period` / `count_pending_in_period` which existed only to feed them. Nothing is hidden, so there is nothing to warn about and nowhere to nudge the user to.

## Rejected

- **Keeping the gate and defaulting the toggles on.** Fixes the empty screen for new reconciles and leaves the underlying confusion — the boxes would still mean two things at once, and a user who turned one off would still get rows vanishing.
- **Bounding the list to the statement dates.** Tempting for tidiness, and wrong for the same reason ADR-040 lets `matched` stragglers show at any date: a transaction the bank posted late belongs on the statement whatever the app thinks its date is. The period governs pre-selection, where a wrong guess is silently accepted, not listing, where the user is looking straight at it.
- **Keeping the parameters as accepted-but-ignored no-ops.** A signature that still takes `include_pending=True` and quietly does nothing is worse than one that rejects it: every call site keeps claiming a filter that no longer happens. A test asserts they raise `TypeError`.
- **Rebuilding the table when a checkbox toggles** (what the old handlers did). Correct when the flags changed the candidate set; now it would be a full reset to move some ticks, discarding the user's hand-placed ones. The in-place re-tick is scoped and reversible.
- **Greying out sub-`matched` rows, or sorting them to the bottom.** Visual gating is still gating; it just makes the rows harder to use rather than impossible.

## Consequences

- **A hand-kept account reconciles normally.** The reported card now lists 40 rows with `Nothing` selected, where it listed none.
- **`pending → reconciled` remains reachable by a deliberate tick**, exactly as ADR-182 made it — but now it takes a click rather than a click plus discovering a checkbox from a banner.
- **The June variance cannot recur by the path it took.** That bug was rows being *ticked* onto a statement, and nothing below `matched` is ever ticked automatically. What changes is that a user who ticks a future pending row by hand is no longer stopped — this ADR treats that as their call, which is the owner's explicit instruction.
- **Long-lived accounts list more rows than before**, including future-dated pending. Ordered by date with search above the table, as it already was.
- **The old default candidate set is gone.** Any future caller of `list_reconcilable_txns` gets everything unreconciled and must filter for itself. The wizard is the only caller.
- No schema change; the confidence ladder, `bank_posted_date` and `close_statement` are untouched.

Verified by driving the real wizard offscreen against `docs/demo data.mfl`: the reported configuration (`Nothing`, both boxes clear) renders a full 40-row table where it previously rendered zero. `tests/test_reconcile_confidence.py` rewritten to the new contract (7 tests: every unreconciled row is a candidate; pending listed at any date; reconciled excluded; this statement's rows included; another statement's not; other accounts' not; the gating kwargs raise) — **3 of them confirmed failing against the pre-change repository**. New `tests/test_reconcile_preselection.py` (11 tests) drives the dialog itself: the screenshot asserted; matched-mode ticks only matched; a toggle moves ticks without moving rows; toggling off is an exact undo; hand-placed ticks survive; pre-selection is date-bounded while listing is not; boxes disabled under `Nothing`; boxes set before Next are honoured; rows carry their status; Missing reaches zero by ticking pending. Full suite 463 passed.
