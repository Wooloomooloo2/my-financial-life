# ADR-024 — Budget core (perimeter + per-category targets + screen)

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design — `category` tree, `txn.transfer_id`); ADR-013 (Category management — hierarchical lookup); ADR-014 (Category kind — drives income vs expense tile math); ADR-018 (Reports framework — strict-outflow semantics on `kind='expense'`); ADR-020 (Account transfers — `transfer_id`-linked pairs, perimeter cancellation depends on this); ADR-022 (Register typeahead — `make_category_picker` reused for the budget setup row dialog); ADR-023 (Scheduled transactions — `list_perimeter_schedules_due_through` reads from the same primitive for planned-spending projection in round C). Second half of the budget arc; ADR-025 will add the visualisations on top.

---

## Context

ADR-023 established **scheduled_txn** as the primitive for known future outflows. This ADR establishes the budget itself: which accounts are in scope, which categories have targets, what cadence those targets work at, and how the per-period numbers are computed and displayed.

The owner's stated requirements (planning conversation 2026-06-05):

> - The budget should allow the user to select the account(s) in scope for the budget. Transfers to accounts outside of the budget must be in the budget. Transfers between accounts where they're both in the budget will be excluded from the budget.
> - The amount of available budget is determined by the net amount of all accounts.
> - The budget screen should show an overall position for the month — carried-forward balance minus actual spending minus planned spending or transfers.
> - Categories can be budgeted weekly / bi-weekly / monthly / quarterly / annually.
> - Income can be budgeted the same as expense.
> - You can budget at sub-category or top level. Sub-categories roll up.
> - You can select which categories to budget at any level.

And from the design-question round:

> - Bills → Budget → Charts build order (this ADR is the Budget round).
> - Single global cadence anchor.
> - Two-line monthly + period total display for non-monthly cadences.
> - Simplifi-style 4-tile summary (Income after bills & saving / Planned spending / Other spending / Available).

This ADR is scoped to the **schema, computation, setup dialog, and main budget screen**. Visualisations (burn-down, donut, sparklines, Budget vs Actual report) are ADR-025's responsibility.

## Options considered

### Budget primitive — one global / one per file / many per file (chosen: one per file in v1; schema supports many)

- *One global*: a single hard-coded budget. Simplest, but breaks the moment a household wants a personal vs. household split.
- **One per file in v1, schema supports many** (chosen): the `budget` table has an id column and the rest of the schema FK-references it, so multi-budget is an additive UI change later. The current UI always loads `get_or_create_default_budget()` — the first existing row, or a fresh one named "My Budget". Multi-budget would add a budget picker dropdown to the screen and to the setup dialog; no migration needed.
- *Many per file from day one*: would need a budget picker, naming UX, and "default budget" decision logic this round. Bigger UI scope for hypothetical benefit; deferred.

### Perimeter shape — `account.in_budget` flag / dedicated M:N (chosen)

- *Flag column on `account`*: cheap but couples the perimeter to the account (one budget per file forever) and complicates the multi-budget future.
- **Dedicated `budget_account` M:N table** (chosen): clean separation, ready for multi-budget, lets a user run multiple overlapping budgets later. ON DELETE CASCADE from both sides so deleting an account or a budget cleans up. Already cheap — perimeter size is bounded by the user's account count.

### Per-category amount sign — signed pence / positive magnitude (chosen)

- *Signed*: income budgets stored positive, expense budgets stored negative. Matches `txn.amount`. Duplicates the information already on the category's `kind`.
- **Positive magnitude, sign inferred from `category.kind`** (chosen): amount is always ≥ 0 in pence. Direction comes from the category. A constraint (`amount >= 0`) on the column. The computation re-signs at display time. Single source of truth for "is this inflow or outflow", and the setup dialog's "Amount" field becomes the natural positive number the user is thinking of (£600/month for groceries, £4,000/month for salary).

### Role tagging — on `category` / on `budget_category` (chosen)

The Simplifi-style "Income after bills & saving" tile needs to know which expense categories are committed (bills, savings) vs. discretionary. The flag could live on either table:

- *On `category`*: a global "this is a bill" property. Survives across budgets. Feels intrinsic ("Netflix is just a bill, period"). Couples to a single semantic across all budgets; future multi-budget runs into "is mortgage a bill in the household budget but a tracked-line in the project budget?" trouble.
- **On `budget_category`** (chosen): each budget decides how it treats each category. Three values — `bills`, `saving`, `discretionary` (default). Multi-budget-ready by construction. The single-budget-per-file v1 doesn't lose anything compared to category-level; the future multi-budget gains the right shape from day one without migration.

### Sub-category rollup — children sum into parent / nearest-budgeted-ancestor (chosen)

The owner's "Sub-categories roll up to the top category" can mean two distinct things:

- *Children always sum into parent*: parents act as buckets that contain the sum of all children's actuals. A budget on the parent caps the *combined* spend. A budget on the children means the parent's display row shows the children's sum. This is the YNAB "category group" shape.
- **Nearest-budgeted-ancestor bucketing** (chosen): every in-perimeter transaction is bucketed against the **nearest budgeted ancestor of its category**, including itself. So:
  - `Food → Groceries → Tesco` with a budget on Groceries → Tesco transactions land under Groceries.
  - Same tree with a budget only on Food → Tesco and Groceries transactions both land under Food.
  - Neither budgeted → land in the "Other" bucket.
  - If both Food AND Groceries are budgeted, Tesco lands under Groceries (the nearest), and direct-on-Food transactions land under Food (its own bucket). The parent's actual does NOT include children that have their own budget — they're already accounted for as their own card.
  - This matches the user mental model "I budget where I budget; everything below it rolls up unless I budget that level too".
- *Always count under self only*: simple, but breaks the "sub-categories roll up to the top" requirement when the user budgets only at the top level.

### Tile arithmetic — net-cash-forward / income-forward (chosen for tiles) + cash-on-hand badge (chosen for header)

The owner described two superficially incompatible models:

> "The amount of available budget is determined by the net amount of all accounts."
> "The budget screen should show an overall position for the month — carried-forward balance minus actual spending minus planned spending."

vs. the Simplifi 4-tile choice:

> Income after bills & saving = planned income − planned bills − planned saving
> Planned spending = sum of discretionary expense budgets
> Other spending = actual spend on un-budgeted categories
> Available = Income tile − Planned spending − Other spending

These are different questions: net-cash-forward asks "is my account balance going to survive the month?", Simplifi asks "am I sticking to my plan?". Both are useful.

- *Pick one and skip the other*: loses one of the two questions the user wants answered.
- **Both — Simplifi tiles for the plan, cash-on-hand badge for the reality** (chosen): the four-tile strip does the Simplifi math; the header shows `Cash on hand: £X across N accounts` as a small reality-check next to the period picker. The tiles are about the plan; the badge is about the bank balance.

### Transfer-kind categories in the tile math — counted as planned outflow / skipped entirely (chosen: skipped)

The spec is unambiguous: "transfers between accounts where they're both in the budget will be excluded from the budget." Excluded means zero effect, planned *and* actual. The intra-perimeter-transfer cancellation in `list_perimeter_txns` handles the actuals correctly, but the tile loop in `compute_budget_view` originally fell through into the bills / saving / discretionary branches for any non-income category — which meant a transfer-kind budget row would subtract from the Income tile via planned_bills / planned_saving / planned_spending and quietly reduce Available with no card-level actual to explain it.

- *Count planned, skip actual*: leaves the inconsistency (the very thing we exclude in actuals shows in planned).
- *Treat transfer-kind as a separate fifth tile*: doubles the tile count for a primitive that's supposed to be invisible to the budget. Rejected.
- **Skip transfer-kind in the tile math entirely; still render the card** (chosen): the card is useful as a tracking surface (the user can plan "I aim to move £500 to savings each month" and see a £500 card with £0 actual when the cancellation worked), but it doesn't move the tiles. External transfers (one half outside the perimeter) skip the tile math too — the in-perimeter half is picked up as a card actual via the standard bucketing rule, and the cash-on-hand badge surfaces the real perimeter-cash impact.

### "Other Spending" sign convention — signed sum / positive outflow magnitude (chosen)

In the Simplifi screenshot "Other spending" is a positive number. With our signed-pence convention, perimeter txns on un-budgeted categories include both incidental inflows (positive) and outflows (negative). Two choices:

- *Signed sum*: matches the math but produces a negative number on the tile when off-plan spending exceeds off-plan inflows. Confusing on a tile labelled "Other spending".
- **Sum of |amount| for negative-amount txns only** (chosen): exact magnitude of off-plan outflow. Inflows to un-budgeted income categories are quietly skipped (they're rare in practice — users who budget income don't leave categories with regular inflows un-budgeted). If real use turns up a case, the v2 fix is to split into "Other spending" + "Other income" tiles.

### Pro-ration for non-monthly cadences — pro-rated only / cadence-period only / two-line (chosen)

The owner chose two-line display for non-monthly cadences (monthly equivalent on top, full-cadence period total on subtitle). v1 implementation:

- **Top line**: card's `period_budget` and `period_actual` are pro-rated to the screen's calendar month. The progress bar tracks this. The "X left / Y over" status is computed from this.
- **Subtitle**: the cadence's native amount + cadence label (e.g. `£15.99 weekly`, `£1,800 annually`). The full-cadence-period actuals + progress (e.g. "this year: £450 of £1,800") are **deferred to ADR-025** since they require a second query window per non-monthly card and that's nicer to land alongside the burn-down + sparkline work.

Monthly cadence cards pass through unchanged (£600/month shows as £600 on a one-month period — see `_is_single_calendar_month`).

### Transfer accounting — pre-filter at SQL / post-filter in Python (chosen: pre-filter at SQL)

The intra-perimeter-transfer cancellation rule needs to exclude transfer rows whose partner is also inside the perimeter:

- *Post-filter in Python*: fetch all perimeter txns, fetch all transfer pairs, filter in code. Round-trips and complexity grow with perimeter size.
- **Pre-filter at SQL with a `NOT EXISTS (… partner …)` subquery** (chosen): one query returns only the txns that should count. The NOT-EXISTS clause checks whether any *other* row sharing the same `transfer_id` has an account in the perimeter. Single round-trip; the existing `idx_txn_transfer` index supports it.

### Period — calendar month / statement / arbitrary (chosen: calendar month)

The owner said "the month" implicitly. v1 ships calendar month only, with prev/next arrows. Statement-period (e.g. credit-card cycles) and arbitrary date ranges are obvious follow-ups but not pre-launch.

### Empty state — auto-create budget / explicit "Set up" CTA (chosen: auto-create then guide)

A fresh `.mfl` file has no budget. The screen could refuse to open ("create a budget first") or auto-create a stub. Auto-create with a clear empty state ("No accounts in this budget yet — click Setup… to choose") is friendlier and matches the rest of the app's "always works, even when empty" character. `get_or_create_default_budget()` lazily inserts the stub on first open.

## Decision

### Schema — migration 0006

Three new tables:

```sql
CREATE TABLE budget (
    id, iri, name, created_at
);

CREATE TABLE budget_account (
    budget_id, account_id  -- composite PK; cascade from both sides
);

CREATE TABLE budget_category (
    id, budget_id, category_id,
    amount    -- positive pence
    cadence   -- weekly | biweekly | monthly | quarterly | annual
    role      -- bills | saving | discretionary
    created_at,
    UNIQUE (budget_id, category_id)
);
```

Two non-unique indexes on `budget_category(budget_id)` and `(category_id)` for the budget-screen and category-deletion paths.

### Repository — new dataclasses + methods

- **Dataclasses**: `Budget`, `BudgetCategoryRow` (joined with category name + parent name + kind), `PerimeterTxn` (id + account + date + signed amount + category id).
- **`BUDGET_ROLES`** module-level tuple (`bills`, `saving`, `discretionary`).
- **`new_budget_iri()`** helper.
- **Budget CRUD**: `get_default_budget`, `get_or_create_default_budget`, `rename_budget`.
- **Perimeter**: `list_budget_account_ids`, `set_budget_accounts` (atomic replace of the perimeter via DELETE + INSERT in one transaction).
- **Per-category budgets**: `list_budget_categories`, `upsert_budget_category` (uses `ON CONFLICT(budget_id, category_id) DO UPDATE`), `delete_budget_category`.
- **Computation source data**: `compute_perimeter_cash_on_hand`, `list_perimeter_txns` (with intra-perimeter-transfer cancellation), `category_parent_map`, `list_perimeter_schedules_due_through` (for ADR-025's planned-but-not-posted projection).

### Computation — `mfl_desktop/budget_calc.py`

Pure-Python module, no Qt, no SQL. One entry point: **`compute_budget_view`** takes the four inputs (`budget_categories`, `perimeter_txns`, `parent_map`, `cash_on_hand`, plus period start/end) and returns `(BudgetSummary, list[BudgetCardData])`.

- **`calendar_month_period(year, month)`** — ISO start/end for a calendar month.
- **`pro_rate_to_period(amount, cadence, start, end)`** — cadence-to-period conversion via average calendar lengths (`365.25/12` etc.), with the matching-cadence-on-its-own-period case (`monthly` on a single calendar month) being an identity pass-through so the typical case shows the entered figure unchanged.
- **`nearest_budgeted_ancestor(category_id, parent_map, budgeted_ids)`** — walks up the parent chain looking for a budgeted node. Returns `None` when the chain reaches root with no budget along the way (→ Other bucket).
- **`compute_budget_view`** — bucketing pass, summary tile math, per-card data assembly. Two passes over `perimeter_txns`: one to build buckets keyed by ancestor, one to sum the "Other" outflow magnitude for the third tile.

Pure functions, fixture-friendly — the round-A repository test pattern (real SQLite in a temp dir) is reusable for future regression checks.

### Setup dialog — `mfl_desktop/ui/budget_setup_dialog.py`

`QTabWidget` with two tabs:

- **Accounts**: `QListWidget` with checkable items, one per non-archived account, family + currency suffix. Pre-checks the current perimeter.
- **Categories**: `QTableWidget` (Category / Cadence / Amount / Role) with Add / Edit / Remove. Sub-dialog `_CategoryRowDialog` for the row edit, with `make_category_picker` (ADR-022) for the category field. Add excludes already-budgeted categories (UNIQUE(budget_id, category_id) would reject duplicates anyway).

Save commits both halves: `set_budget_accounts` followed by per-row `upsert_budget_category` calls and a final delete pass for rows the user removed. Each repo call is atomic on its own; if the categories pass fails after the perimeter pass succeeds, the dialog surfaces the error and stays open so the user can retry — easier to recover from than wrapping both in a giant outer transaction.

### Budget screen — `mfl_desktop/ui/budget_window.py`

Non-modal `QMainWindow`, singleton like the existing report windows. Layout (top-down):

1. **Header strip**: budget name, period label + prev/next month arrows, `Setup…` button.
2. **Cash badge**: `Cash on hand: £X across N accounts` (or the empty-state hint when no perimeter is set).
3. **Four-tile summary**: `_SummaryTile` x 4 (Income after bills & saving / Planned spending / Other spending / Available). Tile colour flips green/red based on a `positive_is_good` flag — Available wants to be big and green; the two spending tiles want to be small.
4. **Card area**: `QScrollArea` of `_CategoryCard`s grouped by role headers (Income / Bills / Saving / Discretionary). Each card has a status label ("£X left" green / "£X over" red), a tinted progress bar (green under budget, red over), and a subtitle showing `X spent of Y · N txns (£Z weekly)` for non-monthly cadences.

`event(WindowActivate)` → `reload()`. Switching back from the register repaints with fresh numbers.

### Wiring — `mfl_desktop/ui/register_window.py`

- New `&Budget` top-level menu with `Open Budget…` (Ctrl+B). Singleton instance held on `self._budget_win` matching the spending-report and net-worth windows; cleaned up on `destroyed`.
- The action is also `addAction`ed to the window so the shortcut fires while the register table has focus.

## Consequences

### Positive

- **One screen captures both "what's my plan" and "what's my cash position"** — the Simplifi tiles for the plan, the cash-on-hand badge for the reality. Avoids forcing the user to choose between the two frames.
- **Perimeter math is correct** — intra-perimeter transfers cancel, cross-perimeter transfers count, and both behaviours hold without any special UI affordance.
- **Pro-rated cards work today, full-period subtitle ships in round C** — the screen is useful immediately for monthly-cadence budgets (the common case), and the two-line non-monthly display has a clear scope for round C without blocking round B.
- **Role on `budget_category`** keeps multi-budget future open — the v1 "this category is a bill" decision is per-budget, not global, so a future split-budget UX doesn't carry baggage.
- **Nearest-budgeted-ancestor bucketing** matches the user mental model from any starting point — budget the leaves, budget the parents, budget both, the cards do what you'd expect each time.
- **`get_or_create_default_budget()` keeps the screen always-openable** — empty state is a guided "set up" prompt, not a hostile error.
- **Computation is a pure function** — `compute_budget_view` takes plain data, returns plain data, runs fast, and is trivial to write fixture-based tests against. The repo smoke test against a real SQLite confirms the wiring end-to-end without Qt in the loop.

### Negative / trade-offs

- **No rollover.** Unspent budget at the end of a period doesn't carry forward; over-budget doesn't deduct from next month. Acceptable for v1 per the planning conversation; a "rollover surplus" flag on `budget_category` is the natural additive future move.
- **Full-cadence-period subtitle deferred to round C.** A user with a £1,800 annual Holidays budget will see "£150/mo" and a monthly progress bar today, but no "this year: £450 of £1,800" subtitle until ADR-025 lands. Mitigated by the cadence label that's already in the subtitle ("£1,800 annually") so the user can do the mental math.
- **One budget per file.** Multi-budget split (household vs. personal) isn't surfaced in the UI yet. Additive change when needed.
- **Two-pass perimeter txn scan in `compute_budget_view`.** Reads the list twice — once for bucket totals, once for the "Other" outflow magnitude. The cost is negligible at MFL's scale; a single-pass refactor is a future micro-optimisation, not a v1 concern.
- **Cash on hand sums across currencies naively.** Same caveat as `compute_account_balances` (ADR-015). Multi-currency perimeters add their pence as if they were one currency. Documented limitation.
- **Setup dialog Save is two atomic operations.** Perimeter and per-category sets aren't wrapped in one outer transaction — if the categories pass fails after the perimeter pass commits, the budget is half-applied. Recovery is "open Setup, fix what's wrong, Save again". Acceptable for a Save-button flow; a future improvement would expose a `set_budget_atomic` repository method that takes both halves at once.
- **`Other spending` tile only counts outflows.** Un-budgeted income categories silently don't show up there. In practice no one budgets income then leaves a regular-inflow category un-budgeted, and the tile label "spending" already excludes income semantically. Split tiles deferred to whenever it bites.

### Ongoing responsibilities

- **`category_parent_map()` snapshots the whole tree per `reload()`.** Cheap at MFL's scale; if categories ever balloon into the thousands, a stub-based bucket assignment in SQL (recursive CTE per perimeter txn, materialised view, or pre-computed ancestor cache) becomes the right move.
- **The Simplifi tile math hinges on `role` being correctly set.** If a user marks their savings category as `discretionary` by accident, the Income tile inflates and Available looks generous. The setup dialog's role combo is the only place to set this; a "did you mean to mark this as a bill?" hint during setup is a future polish item.
- **`pro_rate_to_period` uses `365.25 / 12 = 30.4375` etc. for non-monthly cadences on the calendar month.** Good enough for a budget; if MFL ever grows a "scientific accuracy" requirement (legally-bound forecasts?) this is the place to revisit.
- **Round C (ADR-025) is expected to consume `list_perimeter_schedules_due_through` and add the full-cadence-period subtitle plus burn-down + donut visualisations.** The Repository layer is already shaped for it — no migration needed for round C.
