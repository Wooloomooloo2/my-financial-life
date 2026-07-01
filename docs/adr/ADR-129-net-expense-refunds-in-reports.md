# ADR-129 — Net-expense definition: reimbursements/refunds reduce spend across the flow reports

**Date:** 2026-07-01
**Status:** Implemented
**Supersedes:** the "strict outflow" v1 definition documented on `spending_aggregates` (ADR-018 §spending semantics), which explicitly deferred refund handling to "a future Cash Flow report."
**Related:** ADR-026 (report charts). ADR-064 (Income & Expense), ADR-066 (Payee), ADR-068 (Category & Payee), ADR-056 (Sankey) — the reports sharing the definition. ADR-088 (Income Over Time — the income side, unchanged). ADR-051 (`txn_category_line` split-unroll view). ADR-090 (portfolio-move exclusion, still applied).

## Context

Owner report: a "Reimbursed" (work-expense) category showed **£1,730.79** on the Spending Over Time chart, but drilling in showed a net of only **£45.04** because reimbursement credits offset the expenses.

Cause: five report aggregates defined expense as **strict outflow** — `kind='expense' AND amount < 0`, `SUM(-amount)` — so positive amounts on an expense category (refunds/reimbursements) were silently dropped:

- `spending_aggregates` (Spending Over Time)
- `payee_spending_aggregates` (Payee)
- `category_payee_matrix` (Category & Payee)
- `sankey_category_totals` (Sankey + the Income/Expense donut)
- `income_expense_series` (Income & Expense)

For the owner's category the only stored-negative line was the £1,730.79 card payment; the reimbursement credits were positive and excluded, so the bar showed the gross outflow. The drill-down list (all signed transactions) showed the true net −£45.04, so the bar and its own drill-down could not reconcile. The docstrings had flagged this as a deliberate v1 simplification, but in practice it produced a materially misleading number for any reimbursement-style category, and it meant the report families silently disagreed with each other and with drill-downs.

## Decision

Adopt a **net-expense** definition everywhere the five aggregates compute spend: every `kind='expense'` line contributes its **signed** amount, so a refund reduces its category's spend (`SUM(-amount)` over *all* the category's lines, not just the negative ones). A category (or payee, or category×payee cell) that nets **≤ £0** — refunds meet or exceed outflows — is **clamped to £0** and dropped, because a stacked bar cannot draw a negative segment and the reports promise unambiguously positive bars.

Consistency is the point: the same definition and the same **per-category floor** are used in all five, so Spending Over Time, Payee, Category & Payee, Sankey, and the Income & Expense expense bar all agree with each other and reconcile with a category drill-down. `income_expense_series` was regrouped to include `category_id` so its expense side floors per category (rather than netting at the bucket level) — otherwise a net-credit category would offset other categories and the I&E expense bar would sit below the Spending total. Verified on the demo file: Spending == Income&Expense == Sankey expense totals to the penny.

**The income side is left strict-inflow** (`kind='income' AND amount > 0`; `income_aggregates`, and the income half of Sankey/I&E). Reimbursements are an expense-side workflow; income clawbacks are rare, and netting the income side was neither requested nor needed. The resulting asymmetry (expense nets, income doesn't) is documented on both methods.

Rejected: (a) leaving it strict and only fixing the drill-down to match the gross bar — the drill would then hide the reimbursements the owner explicitly wants reflected; (b) netting only Spending + Payee (the two first discussed) — the other three reports would keep showing the gross £1,730.79, trading one inconsistency for another; (c) bucket-level netting in I&E — simpler, but leaves a per-category-floor gap vs Spending.

## Consequences

- The "Reimbursed" category now reads ~£0 (net) instead of £1,730.79 in every report, and a normal category with a small refund shows its net spend. Refunds are reflected consistently.
- Net-credit categories vanish from the stacked charts (clamped to £0). This is the one accepted lossiness: a stacked positive bar can't show a net credit, so the credit's *own* category reads £0 there — the true signed net is still visible on the category drill-down and register. Because the floor is per-category everywhere (including I&E), the reports still reconcile with each other.
- SQL-only change; no schema, no migration, no data change — purely how existing rows are aggregated. `income_expense_series` now groups by category (finer currency-conversion granularity, same rates).
- Income side unchanged (strict inflow) — documented asymmetry.
- New `tests/test_net_expense_refunds.py` (5/5, Qt-free): a category netting positive keeps its net (£100 − £30 = £70), a category netting negative is dropped, payee/cell clamp likewise, income ignores a negative correction, and the I&E expense equals the spending total. Demo-file check confirms Spending == I&E == Sankey.
- The portfolio-move exclusion (ADR-090) still removes buys (`amount<0` on Uncategorised/expense) from spend — now more important, since without it a buy would net into expense.
