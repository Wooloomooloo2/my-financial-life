# ADR-188 — Transfers count in the budget only when explicitly budgeted

**Status:** Implemented — 2026-08-05

## Context

Owner-reported: the budget shows **Unbudgeted** items under the **Transfers** section. The ask: *don't pick transfers up in the budget at all unless they carry an explicit category that is part of the budget setup.*

Today the budget classifies every perimeter transaction by its category's `kind` (income / expense / transfer) and buckets it to the nearest **budgeted** ancestor category (`nearest_budgeted_ancestor`). A transaction with no budgeted ancestor lands in its section's synthetic **Unbudgeted** row (ADR-058, principle 9). Transfers reach the budget through the ADR-024 perimeter rule: an **intra-perimeter** transfer (both accounts in the budget) cancels in `list_perimeter_txns`, but a **cross-perimeter** transfer (partner account outside the budget) is kept. A cross-perimeter transfer whose transfer-kind category isn't budgeted therefore surfaces as **Unbudgeted / Transfers** — exactly what the owner is seeing (e.g. sweeping money to an external ISA or investment account).

That behaviour was the **ADR-024 spec**, quoted verbatim in that ADR: *"Transfers to accounts outside of the budget must be in the budget. Transfers between accounts where they're both in the budget will be excluded."* This ADR **reverses the first half** of that rule at the owner's request.

## Decision

**A transfer counts toward the budget only when it is explicitly categorised into a category that is in the budget setup** — i.e. only when its nearest budgeted ancestor is not None. An uncategorised or off-plan cross-perimeter transfer is treated as an internal money movement, **not** budget activity, so it never forms an Unbudgeted row.

Concretely: a transfer-kind transaction with no budgeted ancestor is *off-budget* and is dropped from the matrix's Unbudgeted walk. A transfer categorised into a **budgeted** transfer category is unchanged — it still appears under that line (that path never went through the Unbudgeted row).

The rule is one predicate, `budget_calc.transfer_is_off_budget(kind, bucket)` (`kind == "transfer" and bucket is None`), consulted in the two places that must agree:

1. **`compute_matrix`** — the per-section Unbudgeted walk skips off-budget transfers, so the **Unbudgeted Transfers row is never emitted**. With no budgeted transfer line either, the whole Transfers section drops out (it renders only when it has rows).
2. **`budget_window._drill`** — the `section` and `unbudgeted` drills exclude off-budget transfers too, so a Transfers **subtotal drill reconciles** with the (now budgeted-only) subtotal Actual rather than re-including the dropped rows.

Income and expense are untouched: their Unbudgeted rows still surface off-plan activity. The burn-down (expense-only, ADR-058 R3) and the perimeter **cash-on-hand / pool** are unaffected — so the real cash impact of a cross-perimeter transfer is still reflected in the pool badge even though it no longer clutters the envelope grid.

## Rejected

- **Keep the ADR-024 rule (status quo).** It's what the owner explicitly asked to change; an unbudgeted transfer to an external account read as budget "spending" it isn't.
- **A per-budget "include external transfers" toggle.** More surface than the ask; the owner wants the rule, not an option. Can be added later if a use for the old behaviour returns — the predicate is the natural seam.
- **Detect transfers by `transfer_id` in the perimeter query rather than by category kind.** `PerimeterTxn` carries only `category_id`, so the budget already classifies transfer-ness by the category's `kind`; keying off kind keeps the rule in the pure `budget_calc` layer with no query/schema change. (Consequence below.)

## Consequences

- Off-budget transfers no longer appear anywhere in the budget; a transfer is budget activity **only** when categorised into a budgeted category. The Transfers section now contains exactly the transfer categories the user chose to budget.
- The Net line (Σincome − Σexpense − Σtransfer) and the Transfers subtotal both fall to the budgeted-transfer total automatically — they read the section rows, which no longer include the dropped Unbudgeted row.
- **Known limitation (unchanged, now explicit):** because transfer-ness is read from the category `kind`, a transfer with **no category at all** defaults to the expense section (as it always did) rather than being recognised as a transfer. That edge isn't what was reported (the owner's rows carry transfer-kind categories) and closing it would need `list_perimeter_txns` to expose `transfer_id`; left for a follow-up if it ever bites.
- Reverses ADR-024's "cross-perimeter transfers must be in the budget"; the intra-perimeter cancellation and the cash-on-hand accounting from ADR-024 are untouched.

## Verification

`tests/test_budget_transfer_off_budget.py` (3 tests, Qt-free): an off-budget transfer emits no Unbudgeted Transfers row (and no Transfers section when nothing else is budgeted); a transfer categorised into a budgeted transfer category is still counted under that line; unbudgeted income/expense still surface (the change is transfer-only). Existing budget suites (`test_budget_hierarchy`, `_burndown`, `_monthly_row`, `_scheduled_bills`, `_group_collapse`, `_funding_mode`) pass unchanged. No schema change.
