# ADR-068 — Category & Payee report (two-level cross-drill)

**Date:** 2026-06-14
**Status:** Accepted
**Related:** ADR-039 (saved-reports framework — `report.type` enum + per-type filter dataclass). ADR-018 / ADR-030 (Spending Over Time — strict-outflow rule + the category rollup model this borrows `category_group_map` from). ADR-066 (Payee report — its ranked-bar chart, `build_report` ranking, and `TransactionsListWindow` drill, all reused here). ADR-028 / ADR-029 (payee canonical/alias roll-up). ADR-034 (the `TransactionsListWindow` drill-down, which already filters by category *and* payee together). ADR-051 (`txn_category_line` view). ADR-055 (display-currency conversion + exclude-no-rate policy).

---

## Context

Arc E round 3 (E3), the last reports thread. Spending Over Time answers "where does my money go" (by category) and the Payee report (ADR-066) answers "who do I pay" (by payee). The missing view is the **intersection**: within Groceries, *which payees*? and for Tesco, *which categories*? Banktivity-style tools surface this as a cross-tab or a two-level drill.

`report.type` is a hard-listed CHECK (ADR-039 / 0010, widened in 0014, 0023), and `category_payee` was never reserved, so this needs a migration.

The owner picked the shape via `AskUserQuestion`:

1. **Shape → a two-level drill report** (over a cross-tab matrix, or folding the breakdown into the existing Spending/Payee reports). A single focused destination that reuses the Payee report's look: level 1 ranks one dimension, click a row to break it down by the other, click again for the transactions.
2. **Default primary dimension → Category-first** (Payee-first available via a live toggle).

Adopted house defaults (consistent with Spending / Payee, not re-asked): **strict outflow** (`kind='expense'`, `amount < 0`), **transfers excluded** by default with a toggle, a **display-currency selector** (view-only), and a **top-N per level** with a hidden-count note (no "Other" bucket — matching the ADR-066 decision).

---

## Decision

Add a **Category & Payee** report type (`type = 'category_payee'`), reached from **Reports ▸ Category & Payee…** and the New Report… picker. **Migration 0024** widens the `report.type` CHECK (the 0014/0023 table-recreate recipe).

**One aggregate — `Repository.category_payee_matrix`.** A single grouped query returns spending per **(leaf category, canonical payee, currency)** cell, FX-converted to the display currency at period-end (no-rate slices excluded + noted, ADR-055). Strict outflow over `txn_category_line` (ADR-051); payees rolled to canonical via `COALESCE(p.canonical_id, p.id)`; transfers dropped unless included. Returning the *matrix* (not a pre-pivoted list) lets the window pivot, roll up, and drill **in-memory without re-querying** — toggling the primary dimension or drilling is instant.

**The window — `category_payee_window.py`.** A "Group by: Category / Payee" top-bar toggle sets the **primary dimension**; the **drill** ((item id, name) or none) is view-only state.

- **Level 1** ranks the primary dimension across all cells: category cells roll up to the **budget-line group** (`category_group_map` — Groceries, Transport…, *not* the Income/Expense roots `category_root_map` would give), payee cells group by canonical payee. Clicking a row drills.
- **Level 2** ranks the *other* dimension within the drilled item (cells filtered to that category group, or that payee). Clicking a row opens the transactions.
- **Transactions** open in the shared `TransactionsListWindow` (ADR-034) with **both** filters set — the category group's descendants (`category_descendants`) and the payee expanded back to canonical + aliases (`expand_canonical_payee_ids`), or the NULL-payee group. A **← Back** button pops level 2 to level 1; a breadcrumb shows the path.

**Reuse.** The ranked-bar chart (`PayeeChart`), the ranking/top-N/hidden-count (`payee_report.build_report` → `PayeeSpendRow`), the sortable-table pattern, and the drill-down window are all reused. `PayeeSpendRow.payee_id` carries "the current row's item id" whichever dimension it is (a category-group id or a canonical-payee id) — the window documents the mapping. The display currency and drill are view-only; `group_by`, period, accounts, top-N, and the transfers flag persist in `CategoryPayeeFilters`.

---

## Options considered

- **Two-level drill report (chosen)** vs. a **cross-tab matrix grid** vs. **folding into the existing reports**. The drill is one discoverable destination, reuses the Payee look, and degrades gracefully to many rows; a matrix is dense and fits poorly as the account/payee count grows, and folding spreads the feature across two reports with no single home. (Owner pick.)
- **Category dimension = budget-line group (`category_group_map`) (chosen)** vs. leaf categories vs. roots. Roots collapse everything to "Expense"; leaves explode into a long list; the budget-line level (Groceries, Transport) is the natural reading and matches the Spending report's mental model. The transaction drill still uses `category_descendants(group)` so nothing under the group is missed.
- **Return the matrix, pivot in the window (chosen)** vs. a query per view/drill. One query feeds every pivot and drill with no extra round-trips; the matrix is small for a personal file.
- **Reuse `PayeeChart` / `build_report` (chosen)** vs. a bespoke chart/ranker. The Payee report's ranked bars + top-N + hidden-count are exactly what each level needs; reuse keeps the new surface area to one repo method, one window, one filter dialog (+ the type plumbing).
- **Category-first default (chosen)** vs. payee-first — owner pick; the toggle flips live either way.

---

## Consequences

### Positive
- Closes the reports arc's last cut — the category×payee intersection, drillable in both directions, ending in the actual transactions.
- Small footprint despite the capability: one aggregate, one window, one filter dialog, reusing the chart/ranker/drill-down already shipped for E2/ADR-034.
- Currency, strict-outflow, canonical roll-up, and transfer handling are identical to the sibling reports, so behaviour is predictable and the figures reconcile with the Payee and Spending reports.

### Negative / trade-offs
- **Two levels only** — you can't chain category → payee → sub-category; the second click goes to transactions. That's the chosen scope; deeper nesting would muddy the model.
- **Top-N applies per level** and truncates silently in the chart (the summary's hidden-count note flags it); the grand total/percentages are over all rows at that level.
- **The reused `PayeeSpendRow.payee_id` field carries a category id** when the row dimension is category — functional but a slight naming smell, documented in the window. A future rename to a neutral `item_id` would tidy it across E2/E3.
- **Drill-down account scope is coarse** (inherited from ADR-034/066): a single selected account is exact; an account *subset* opens the cross-account transactions view, which can over-include. All-accounts and single-account are exact.
- **Budget-line grouping hides the leaf** in the report itself — e.g. Groceries lumps its sub-categories; the leaf is visible only once you reach the transactions. Acceptable for a cross-cut overview.

### Ongoing responsibilities
- A new report type still means: a migration to widen the CHECK, a filter dataclass + registration, and New Report / dispatch / menu entries — the checklist this followed.
- If E2/E3 ever want a shared generic ranked-row type, rename `PayeeSpendRow.payee_id` → `item_id` in one pass and drop the "id carries either dimension" note.
