# ADR-140 — Income & Expense: choose which transfers to fold in

**Date:** 2026-07-05
**Status:** Implemented
**Related:** ADR-064 (Income & Expense report). ADR-056 (`sankey_category_totals`, shared with the composition donut). ADR-129 (net-of-refunds expense definition). ADR-051 (`txn_category_line` split-unroll view). ADR-139 (split-line transfers — a mortgage principal buried in a split is captured here too, via the view).

## Context

The owner is building a **rental ROI** view in the Income & Expense report: rental income, operating expenses, mortgage interest, and the mortgage **principal**. In this app a mortgage principal payment is a **transfer** (to the mortgage account), not an income/expense category.

Two problems with the report today:
1. The report's kind rule (`(kind='income' AND amount>0) OR kind='expense'`) means `kind='transfer'` legs are **never** counted — so "Show transfers" (the `include_transfers` bool) could only ever surface transfer legs *mis-filed under income/expense categories*, never a proper transfer. A mortgage-principal transfer could not appear at all.
2. Even if it could, "Show transfers" is **all-or-nothing** — there's no way to say "include the mortgage transfers but not every inter-account move."

## Decision

When `include_transfers` is on, fold `kind='transfer'` legs into the report as **directional cash flows**, and let the user pick **which** transfer categories via a new checklist.

- **Directional treatment** (owner's pick): a transfer *outflow* (`amount < 0`) counts on the **expense** side, an *inflow* on the **income** side. Unlike an expense category, transfers are **not netted** — each leg counts by its own sign (a single-sign group per direction, so it's always a positive magnitude and never hits the ADR-129 £0 floor). Net income − expense then reads as the ROI cash flow.
- **Which transfers** (owner's pick — by category): `IncomeExpenseFilters` gains `transfer_category_ids` (empty == all transfer categories). `income_expense_series` and `sankey_category_totals` (the composition donut) gain matching params; both add an `OR (kind='transfer' AND category_id IN (…))` branch and a `flow` CASE that classifies each leg by direction, keyed by its own category so it rolls up as its own slice. The income/expense category narrowing is exempted for transfer legs so it can't drop them.
- **Scope note**: a transfer has two legs (out of one account, into another). Both are counted only if both accounts are in scope; scoping the report to the operating account(s) — the natural ROI setup — leaves just the outflow leg, so the counterpart (e.g. the credit into the mortgage) doesn't show as phantom income. The filter dialog's transfer panel spells this out.
- **UI**: the filter dialog gains a third checklist, **"Transfer categories (empty = all)"**, listing `kind='transfer'` categories, enabled only while "Include transfers" is ticked. The summary line reads "Transfers: included (N categories)".

Defaults are unchanged: `include_transfers=False` ⇒ no transfers (the cash-flow-correct default), and the new params default off, so the **Sankey report** (which shares `sankey_category_totals`) is untouched.

Rejected: by-account selection (the owner preferred by-category — it names the *purpose*, e.g. "Mortgage Principal", independent of how many accounts are involved); a separate "Transfers" series (the owner wanted them folded into the flows so the net reads as ROI); netting transfer categories like expenses (a transfer is directional, not a refundable expense).

## Consequences

- The Income & Expense report can now express an ROI view: with the report scoped to the rental's operating account and "Mortgage Principal" ticked, rental income sits on the income side; repairs, interest, **and the principal transfer** on the expense side; the net is the property's cash flow. The composition donut matches the bars (both read `sankey_category_totals`), so "Mortgage Principal" shows as its own expense slice.
- To get per-purpose granularity the user files transfers under distinct transfer categories (e.g. reconciling mortgage transfers under "Mortgage Principal", ADR-139). A generic "Transfer" category still works but lumps everything.
- No schema change or migration. `include_transfers` keeps its meaning as the master enable; `transfer_category_ids` narrows it. Old saved filters (no `transfer_category_ids`) default to empty = all, matching the prior "include all transfers" intent.
- `tests/test_income_expense_transfers.py` 6/6 (default excludes; only the picked category folds in — £810.26 expense not £1,110.26; empty = all; the composition totals carry the transfer category; JSON round-trip + old-blob default; the dialog panel enables with the checkbox and captures the pick). Full suite 30/30; filter dialog screenshotted.
