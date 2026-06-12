# ADR-058 — Budget redesign: hybrid envelope / zero-sum / long-term planning, per-month allocations, 12-month matrix

**Date:** 2026-06-12
**Status:** Accepted (umbrella). **R1 shipped 2026-06-12**; R2–R4 are scoped rounds under this ADR.
**Supersedes:** ADR-024 (budget core — perimeter + per-category `amount + cadence` + Simplifi screen), ADR-025 (budget visualisations — burn-down + proportional bar + cadence subtitles). The good bones of both — the **perimeter cancellation rule**, **nearest-budgeted-ancestor bucketing**, and the **pure `budget_calc.py`** shape — carry forward; their single-amortized-amount model and Simplifi-tile screen do not.
**Related:** ADR-010 (`category` tree, `txn.transfer_id`); ADR-014 (category `kind` drives income/expense/transfer sectioning); ADR-016/057 (auto-commit + snapshots — every matrix edit persists immediately); ADR-018 (no-pies rule — charts stay paintEvent); ADR-023 (`scheduled_txn` — planned-but-unposted projection); ADR-026 (paintEvent charts, `chart_helpers`); ADR-035/055 (multi-currency — the perimeter pool needs `convert_amount`); ADR-051 (`txn_category_line` split-unrolling view — actuals read through it).

---

## Context

The budget arc shipped in three rounds (ADR-024 core, ADR-025 visualisations, ADR-026 charts) and was always flagged as **rough — needing rethinking after real use**. After living with it, the owner re-stated the budget's principles, and they amount to a re-design rather than a polish pass. The twelve principles (planning conversation 2026-06-12):

1. **Multiple budgets** — one budget per file is too constraining; users want separate budgets and want to try out **scenarios**.
2. **A genuine hybrid** of traditional **envelope** budgeting, **zero-sum** budgeting, and **long-term planning**. Every app picks one and neglects the others.
3. **Accounts are picked first; the available budget is the balance of those accounts.** (Future: let a credit card's available credit be part of a budget, and add a pay-down-credit-cards feature.)
4. **Budgets have a period.** Default Jan–Dec.
5. **The user selects categories**; the budget may **suggest amounts from historical spending**.
6. **Roll-up budgeting that still tracks children** — budget £500 for "Bills" and still track electric / gas / council tax underneath.
7. **Rollover** — unspent carries, or the user **takes the excess and allocates it elsewhere** (saving, another category).
8. **A 12-month matrix** showing the whole budget, with **Budget / Actual / Diff** lines for income, expenses and transfers.
9. **Unbudgeted spending is shown as such.**
10. **The matrix is editable** — amend budgets on the fly.
11. The matrix is the default **annual view**; there is also a **monthly view** with traditional charts of progress, also editable.
12. A **burn-down chart** for any category or the whole budget — like Pocketsmith's, but better: Pocketsmith assumes spending stops at "today", whereas ours **projects the burn-down forward at the current rate of spending**.

The fundamental tension with the existing design: ADR-024 stores one **amortized `amount + cadence`** per budgeted category and pro-rates it to a single calendar month. The matrix (edit *any* month — principle 10) and rollover (principle 7) **cannot be derived** from one amortized figure. The model has to pivot to **explicit per-month allocations**.

## Decisions

The owner resolved the load-bearing forks directly (2026-06-12). Each is recorded with the alternatives considered.

### D1 — Allocation model: per-month rows + copy-forward on edit (chosen)

- *Keep cadence-amortized (ADR-024):* one `amount + cadence`, matrix is a read-mostly projection. **Rejected** — can't truly edit a single month or do clean rollover (kills principles 7 & 10).
- *Template + per-month overrides:* a persistent recurring default plus per-cell overrides. Workable, but carries a second source of truth (template vs override) and the "which wins" ambiguity.
- **Per-month storage + copy-forward affordance (chosen).** Every month's figure is its own `budget_allocation` row — the matrix is the **native, fully-editable object**, no template to reconcile. Editing a cell offers how far to propagate: **Just this month** / **This + all later months** / **All 12 months**. So `£500` typed in January can stamp all twelve; a `£300→£500` correction in June can stamp June-onward without silently rewriting months already tuned. Propagation is a *write-time convenience*, not stored state.

### D2 — Available pool + zero-sum strictness: soft indicator (chosen)

The available pool is the **summed balance of the perimeter accounts** (principle 3), converted to the budget's display currency via `convert_amount` (ADR-055 — no naive par-add). On top of that:

- *Strict YNAB-style:* "To Be Budgeted" must reach £0; over-assignment warns/blocks. **Rejected** — naggy, and against the app's "always works, never scolds" character.
- *Per-budget toggle.* Deferred — can be added additively if a live budget ever wants discipline a scenario doesn't.
- **Soft indicator (chosen).** Show `Unallocated = pool − assigned-this-month` as guidance; over-assigning turns it red but **never blocks**. Envelopes may exceed the pool. This is the hybrid's zero-sum *flavour* without the zero-sum *enforcement*.

### D3 — Rollover + reallocation: auto-rollover, reallocate by editing (chosen)

- *Manual only, opt-in:* envelopes reset monthly unless a line opts in. **Rejected** as the default — most expense envelopes want carryover.
- *Auto-rollover + a tracked `budget_move` ledger:* surplus carries, and reallocation is a first-class auditable "move £50 from Groceries surplus to Savings". **Rejected for now** — extra schema + UI for a flow the matrix already expresses.
- **Auto-rollover + free editing (chosen).** A per-line `rollover` policy (default **on for expense lines**, off for income/transfer) carries the running surplus/deficit forward automatically. To **reallocate**, the user just edits the matrix cells (with the copy-forward affordance) — no separate move concept. `carry_in[m]` is **computed**, never stored: `carry_in[Jan] = 0`, `carry_in[m+1] = allocation[m] + carry_in[m] − actual[m]` for `rollover='accumulate'`, else `0`. Carry runs in both directions (an overspend reduces next month) — clamping-at-zero is a later tweak if it bites.

### D4 — Roll-up budgeting with child tracking (chosen — carried from ADR-024)

ADR-024's **nearest-budgeted-ancestor bucketing** already *is* principle 6: budget "Bills", and electric/gas/council-tax (its descendants, un-budgeted) bucket up into it. The redesign keeps the rule unchanged and adds the **UI affordance**: a budgeted matrix row is **expandable** to show its tracked children as read-only actual sub-rows. Budget a child too and it becomes its own row (and stops rolling into the parent) — exactly today's semantics.

### D5 — Sectioning by `kind`; unbudgeted shown explicitly (chosen)

Matrix rows are grouped into **INCOME / EXPENSES / TRANSFERS** sections by the line category's `kind` (principle 8). Each section carries a synthetic **"Unbudgeted"** row (principle 9) — the perimeter txns in that section with no budgeted ancestor (the old `OTHER_BUCKET`, now surfaced per-section per-month instead of one "Other spending" tile). Transfers remain perimeter-cancelled (intra-perimeter nets to zero; cross-perimeter counts) per ADR-024/020.

### D6 — Suggested amounts from history (chosen)

When adding a category to a budget, offer a suggested allocation = the **trailing-12-month average** of actual spend on that category (and descendants), via the existing spending aggregates. The user accepts or overrides. Seeds principle 5; never auto-applied without a click.

## Data model — migration 0019

`budget_category` (ADR-024) is replaced. The migration **extends `budget`**, **adds `budget_line` + `budget_allocation`**, migrates existing rows, and **drops `budget_category`**.

```sql
-- budget gains a period (principle 4). start_month is 'YYYY-MM'.
ALTER TABLE budget ADD COLUMN start_month   TEXT;     -- e.g. '2026-01'; backfilled below
ALTER TABLE budget ADD COLUMN length_months INTEGER NOT NULL DEFAULT 12;
ALTER TABLE budget ADD COLUMN currency      TEXT;     -- display currency for the pool; NULL = file base

-- The envelope: one row per budgeted category in a budget (replaces budget_category).
CREATE TABLE budget_line (
    id          INTEGER PRIMARY KEY,
    budget_id   INTEGER NOT NULL REFERENCES budget(id)   ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES category(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'discretionary'
                  CHECK(role IN ('bills','saving','discretionary')),
    rollover    TEXT NOT NULL DEFAULT 'none'
                  CHECK(rollover IN ('none','accumulate')),  -- repo seeds 'accumulate' for expense lines
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (budget_id, category_id)
);

-- The editable matrix cell: per-line, per-month allocation (positive pence; sign from category.kind).
CREATE TABLE budget_allocation (
    id              INTEGER PRIMARY KEY,
    budget_line_id  INTEGER NOT NULL REFERENCES budget_line(id) ON DELETE CASCADE,
    month           TEXT NOT NULL,                              -- 'YYYY-MM'
    amount          INTEGER NOT NULL CHECK(amount >= 0),
    UNIQUE (budget_line_id, month)
);

CREATE INDEX idx_budget_line_budget       ON budget_line(budget_id);
CREATE INDEX idx_budget_line_category     ON budget_line(category_id);
CREATE INDEX idx_budget_allocation_line   ON budget_allocation(budget_line_id);

-- Data migration: budget.start_month := Jan of the current year; one budget_line per old
-- budget_category; seed 12 monthly allocations from the old amortized amount (rough cadence→month).
UPDATE budget SET start_month = strftime('%Y-01','now') WHERE start_month IS NULL;
-- (budget_line + budget_allocation seeding done in the migration body — see R1.)
DROP TABLE budget_category;
```

**Nothing about actuals or rollover is stored** — both are computed in `budget_calc.py` from `budget_allocation` + perimeter txns + prior-month carry. `budget_account` (the perimeter M:N) is unchanged; the credit-inclusion flag is an R4 concern.

## Computation — `budget_calc.py` (rewritten, still pure)

The pure-function contract survives; the shapes change from "one period, tiles + cards" to "a month grid".

- **`MonthGrid` / `MatrixRow`** dataclasses: per `budget_line`, a vector over the budget's months of `{allocation, actual, carry_in, available, diff}`, plus the line's category/kind/role/rollover and its (lazy) child-tracking rows.
- **`compute_matrix(budget, lines, allocations, perimeter_txns, parent_map, today)`** — the new main entry point. Buckets perimeter txns to the nearest budgeted ancestor **per month**; folds carry forward per D3; assembles INCOME/EXPENSES/TRANSFERS sections each with a synthetic **Unbudgeted** row; returns the grid + section subtotals + the **Unallocated** (pool − assigned) figure per month.
- **`compute_burndown(line_or_budget, perimeter_txns, allocation_total, period, today)`** (R3) — cumulative outflow vs ideal pacing **plus a projection** that extends the observed average daily rate from today to period end (principle 12), so the line crosses zero early when overspending instead of going flat.
- Reuses `nearest_budgeted_ancestor`, `pro_rate_to_period` (now only for seeding/suggestions), and the perimeter primitives. `convert_amount` is applied to the pool (D2) and to any cross-currency perimeter.

## UI surfaces

1. **Budget picker** (principle 1) — a dropdown / sidebar entry to select among budgets; **New**, **Duplicate as scenario**, **Rename**, **Delete**. Removes ADR-024's `get_or_create_default_budget` singleton assumption.
2. **Setup** (principles 3, 5) — pick perimeter accounts first (pool shown), then pick categories with **suggested amounts** (trailing-12-month average), role, and rollover policy.
3. **Annual matrix** (principles 8–10, default view) — rows = budget lines grouped by kind section, expandable to child tracking rows (D4); 12 month columns; each cell shows **Budget / Actual / Diff** (Budget editable inline, copy-forward affordance on commit per D1); section subtotals + per-section **Unbudgeted** rows; an **Unallocated** indicator per month (D2). A `QTableView` + custom model is the natural fit.
4. **Monthly view** (principle 11) — one month in focus, traditional progress charts (paintEvent per ADR-018/026), budgets editable here too.
5. **Burn-down** (principle 12) — per-line or whole-budget, with the forward projection. Replaces ADR-025's `burn_down_chart.py` shape.

## Phasing

Each round is its own implementation pass under this ADR (the schema lands once, in R1):

- **R1 — Foundation + editable matrix. ✅ Shipped 2026-06-12.** Migration 0019 (`budget_line` + `budget_allocation`, migrate + drop `budget_category`); Repository layer (`list/get/create/duplicate/delete_budget`, `set_budget_period/currency`, `add/update/delete_budget_line`, `list_budget_allocations`, `set_line_allocation` with atomic copy-forward scope `one`/`forward`/`all`, `compute_perimeter_pool` w/ ADR-055 conversion, `historical_monthly_average`, `category_kind_map`); `budget_calc.compute_matrix` (pure — per-line × per-month allocation/actual/carry/available/diff, nearest-ancestor bucketing per month, auto-rollover folding both directions, per-section Unbudgeted rows, section subtotals, `assigned_by_month` for the soft Unallocated); `budget_window.py` rewritten as the matrix (`BudgetMatrixModel` over a `QTableView` — Income/Expenses/Transfers sections, Budget/Actual/Diff lines per envelope, editable Budget cells routing through a copy-forward scope prompt, today-month tint, diff red/green, soft Unallocated + missing-rate banner, budget picker with New/Duplicate/Rename/Delete/Period…/Set up…); `budget_setup_dialog.py` rewritten (perimeter + envelope lines with role + rollover + seed-from-history). Old `burn_down_chart.py` removed (R3 rebuilds a projected one). **Verified** against fresh + replayed-migration + a copy of the live dev DB, plus offscreen-Qt construction/edit/copy-forward. **As-built notes:** the soft Unallocated is shown for the *focused* (today's) month rather than per-cell; the annual matrix lands first, the monthly view is R3; child-tracking *expansion* in the matrix is R2 (the bucketing already rolls children up). Auto-rollover math is live from R1 (intrinsic to the grid); surplus-reallocation is just cell editing.

  **R1 refinements from first real-use (2026-06-12):** (a) **Diff is signed so positive is always favourable** (`_favourable_diff`) — income is favourable when actual ≥ budget (you earned over plan), expense/transfer when actual ≤ budget; the earlier raw `available − actual` showed unbudgeted income as a big red −£17,939 "shortfall". The raw surplus (`available − actual`) still drives rollover carry regardless of display sign. (b) **A section whose only row is Unbudgeted (or a single budgeted line) omits the subtotal** — it would just duplicate that row and read as double-counting. (c) **The Unbudgeted row shows "—" for Budget** (it has no allocation), not an editable 0. (d) **Setup's Add-categories flow lists *all* categories with their transaction-usage counts** (`Repository.category_usage_counts`), most-used first, multi-select — so the user can see what they have rather than recall category names. (e) **Double-clicking an Actual cell drills into the exact transactions behind it** (`budget_drilldown_window.py`) — the set is recomputed with the same nearest-ancestor bucketing / transfer-cancellation / Unbudgeted rules so it reconciles with the cell (a Groceries drill includes its rolled-up Tesco children; the Unbudgeted row drills to exactly the off-plan txns; a section subtotal drills to the whole section/month). Rows are an editable register (same typeahead delegates), so recategorising-to-tidy flows back through the Repository and the matrix updates on its next activation refresh.

  **macOS "budget window vanishes on Save" bug (2026-06-12, fixed).** Saving the Setup dialog (especially after the nested Add-categories chooser) made the whole budget window disappear on macOS — with *no* Qt close or destroy event, so nothing logged. Root cause: the budget window was opened as a **child of the register window** (`parent=self`), and macOS automatically **hides a child window whenever its parent isn't the key window**; when a modal dialog took key focus, the register lost key and the OS hid the (child) budget window. Fix: open the budget window as an **independent top-level window** (`parent=None`) — it then stays visible behind an app-modal dialog. Its own dialogs (Setup, chooser, edit, copy-forward, period, New/Duplicate/Rename/Delete) are likewise **`parent=None`** to avoid the inverse macOS quirk where a *child modal* closing cascades a spurious `Close` to its parent window. **Lesson for the codebase:** a secondary window that itself spawns modal dialogs should be top-level (`parent=None`), not a child of the main window. Three real robustness bugs were fixed alongside: the activate-refresh crashing on a closed DB during shutdown (now `Repository.is_open()`-guarded), a model **swap** orphaning open inline editors (now an in-place `set_matrix` reset), and `_render` being able to raise into a Qt event handler (now fully swallowed + logged). (f) **Prepopulation** — a setup "Populate from history…" verb pre-ticks the **top-level** categories you've actually used in the chosen accounts over the last 12 months (`Repository.top_level_categories_with_activity`, owner picked group-level over per-leaf so a fresh budget is ~15 clean lines with children rolling up), each seeded from its average; the chooser now shows **rolled-up** usage counts (`category_rollup_usage_counts`) so a parent reflects its subtree rather than reading "0 txns", and suggestions float to the top, pre-ticked, for review-and-adjust. (g) **Second real-use pass:** the Add-categories chooser is a **nested tree** (parent→child, alphabetical within each parent, already-budgeted nodes greyed and un-tickable) rather than a flat usage-sorted list; **role is hidden for income** categories (it's a bills/saving/discretionary expense concept — Edit dialog omits it, the setup table shows "—", the chooser's default-role is labelled "(expenses)" and income lines store a no-op default); and a **rollover indicator** annotates any Budget cell carrying rolled-in surplus/deficit — the cell shows `400.00  (+50.00)`, an amber tint, and a tooltip ("Budgeted 400.00 + 50.00 rolled over = 450.00 available"), so Budget/Actual/Diff visibly reconcile instead of the Diff looking wrong-by-a-bug (the edit value stays the raw allocation).
- **R2 — Rollover polish + child-tracking expansion.** Per-line rollover toggles in the UI, the expandable child-tracking sub-rows, carryover display/affordances.
- **R3 — Monthly view + projected burn-down.**
- **R4 — Credit cards as a budget source + pay-down goals** (the principle-3 future features).

## Consequences

### Positive
- **The matrix is the model, not a view** — per-month allocations make "edit any cell" and rollover first-class instead of bolted on.
- **One dataset, three surfaces** (matrix / monthly / burn-down) all read the same `budget_allocation` + perimeter primitives — no divergence.
- **Genuinely hybrid:** envelopes (lines + rollover) + zero-sum flavour (soft Unallocated against the real account pool) + long-term planning (the 12-month grid).
- **Carries forward what worked:** perimeter cancellation, nearest-ancestor bucketing, pure `budget_calc`, paintEvent charts — the redesign is a re-shape, not a teardown.
- **Multi-budget + scenarios** fall out of the schema the moment the singleton assumption is dropped.

### Negative / trade-offs
- **A real migration with data loss risk.** Existing budget_category rows are reshaped into lines + 12 seeded monthly allocations via a rough cadence→month conversion; the owner re-tunes. Acceptable given the current budget is "rough" and being replaced.
- **More rows.** A 30-line, 12-month budget is 360 `budget_allocation` rows — trivial for SQLite, but the matrix recompute touches all of them each refresh. Fine at MFL scale; a per-budget cap on recompute is a future concern, not a v1 one.
- **Rollover carries deficits too.** An overspent month reduces next month's available; some users expect overspend to clamp at zero. Documented; a per-line clamp option is an easy later add.
- **Bigger surface to build** — four rounds. R1 alone is migration + repo + calc + a non-trivial matrix widget. Phasing keeps each reviewable.

### Ongoing responsibilities
- **`compute_matrix` is the new single source of budget truth** — the monthly view (R3) and burn-down must read from it, never re-derive actuals independently, or the surfaces will disagree.
- **Currency:** the pool and cross-currency perimeters go through `convert_amount` (ADR-055); any new total must not re-introduce the par-add bug.
- **The copy-forward affordance writes many rows in one commit** — it must be one atomic Repository call so a partial stamp can't leave a half-propagated row.
- **Credit-card source (R4)** will extend the pool definition (available credit) and `budget_account` (an inclusion flag) — keep the pool computation in one place so R4 is additive.
