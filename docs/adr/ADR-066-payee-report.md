# ADR-066 — Payee report (ranked spending by payee)

**Date:** 2026-06-14
**Status:** Accepted
**Related:** ADR-039 (saved-reports framework — `report.type` enum + per-type filter dataclass + sidebar/Save flow this plugs into). ADR-018 / ADR-030 (Spending Over Time — the strict-outflow definition and the report-window shape this mirrors). ADR-028 / ADR-029 (payee canonical/alias model — the roll-up this report depends on). ADR-051 (`txn_category_line` split-unrolled view). ADR-035 / ADR-055 (multi-currency FX layer + the exclude-no-rate display policy). ADR-064 (Income & Expense — the sibling Arc E report whose display-currency selector and transfers toggle this reuses verbatim).

---

## Context

Arc E (the reports arc) round 2. After Spending Over Time (by category, ADR-018/030) and Income & Expense (cash-flow over time, ADR-064), the obvious missing cut is **"who do I pay the most?"** — spending ranked by *payee* rather than category or time bucket. Banktivity and every personal-finance tool ships one; MFL had the data (every transaction carries a payee) but no view of it.

`report.type` is a hard-listed CHECK constraint (ADR-039 / migration 0010, last widened in 0014). Unlike `income_expense` — which ADR-039 had reserved in the original 0010 list so ADR-064 needed no migration — **`payee` was never reserved**, so this report needs a migration to widen the CHECK before a `payee` row can be saved.

Four design forks were put to the owner via `AskUserQuestion`:

1. **Measure** → *Spending only (strict outflow)*. Over a two-sided income/expense view or a signed-net view. "Who I pay the most" is the question; strict outflow (the ADR-018 rule: `kind='expense'` AND `amount < 0`) keeps the bars unambiguously positive and stops income/refunds muddying the ranking — exactly as Spending Over Time does.
2. **Visual** → *Ranked bars + sortable table together*. Horizontal bars give the at-a-glance "biggest payees" read; the table underneath gives the precise figures and re-sorting (by spend, %, or transaction count). Both the bars and the table rows are **clickable** to open that payee's transactions (added after a review of the first cut — see below).
3. **Aliases** → *Roll up to canonical*. "AMZN" and "Amazon UK" must count as one payee (ADR-028/029), via `COALESCE(p.canonical_id, p.id)`.
4. **Transfers** → *Exclude, with a toggle* (default exclude) — the same pattern and default the owner chose for Income & Expense (ADR-064), for consistency.

---

## Decision

Add a **Payee** report type (`type = 'payee'`), reached from **Reports ▸ Payee…** and the sidebar's New Report… picker.

**Migration 0023** rebuilds the `report` table with `'payee'` added to the `type` CHECK — the same table-recreate dance 0014 used (SQLite can't `ALTER` a CHECK in place; `foreign_keys=OFF` during the swap; indexes recreated).

**Aggregation — `Repository.payee_spending_aggregates`.** Strict outflow (`kind='expense'` AND `t.amount < 0`) over the split-unrolled `txn_category_line` view (ADR-051), grouped per **canonical payee** via `COALESCE(p.canonical_id, p.id)` and labelled with the canonical name (a self-join on `payee`). Lines with no payee collapse to a single `payee_id = None` group. `include_transfers=False` (default) additionally drops any line carrying a `transfer_id` (a linked transfer leg) — mirrors `income_expense_series`. `display_currency` converts each per-(payee, currency) slice via the ADR-035 FX layer at the period-end date; per ADR-055 a slice with no rate is **excluded and noted** (`unconverted`), never par-added.

**Pure ranking — `reports/payee_report.py`.** `build_report(raw_payees, top_n)` sorts payees by spend descending (pence → major-unit `Decimal`), attaches each row's share of the grand total, and **keeps the top `top_n`** (`top_n = 0` shows everything). The tail past the cap is simply **not shown — there is no "Other" bucket** (an owner review of the first cut found the "Other" row more noise than signal); the summary reports `hidden_count` so the truncation is explicit ("Showing top 15 of 73 payees (58 hidden)"). Percentages and the summary `total` are over **all** payees, shown or hidden, so each row's pct is its true share of spend (the shown rows can therefore sum to under 100%). Returns the display rows plus a summary (grand total, distinct/shown/hidden counts, top payee). No Qt, no SQL — fully verifiable offscreen.

**UI.** `payee_chart.py` — a hand-rolled horizontal ranked-bar `paintEvent` widget (ADR-026; longest bar at top; a single accent hue so colour doesn't falsely encode rank; hover tooltip with amount/%/count). `payee_report_window.py` — the standard report window (top bar with display-currency selector + Filter/Save/Save As; chart over a sortable detail table on the left; summary panel on the right; the ADR-039 bare/saved + dirty/close lifecycle). `payee_filter_dialog.py` — period preset (+ custom range), a "Show top N" spin (0 = all), an "Include transfers" toggle, and an accounts checklist. The display currency is a view preference (not persisted), matching Net Worth / Sankey / Income & Expense.

**Drill-through to transactions.** Clicking a bar or double-clicking a table row opens the existing `TransactionsListWindow` (ADR-034) scoped to that payee over the report's current date range. Because the report rolls aliases up to the canonical payee, the click **expands back to the full id set** (`expand_canonical_payee_ids` — canonical + aliases) so the drill-down lists exactly what the bar counted; the "(No payee)" group drills to transactions with a NULL payee. This required widening the shared drill-down: `TxnListFilter.for_payees` (a `payee_ids` set + a `payee_is_null` flag) and matching `DrillDownFilterProxy.set_payee_ids` / `set_payee_null` — the existing single-id path is untouched. Account scope: a single selected account opens the per-account view; all-accounts or an account *subset* opens the cross-account view (the drill-down can't represent an account subset — see trade-offs).

---

## Options considered

- **Measure: spending-only (chosen)** vs. two-sided income+expense vs. signed-net — see Context fork 1. Strict outflow matches the report's question and the Spending Over Time precedent; a payee that both bills and refunds you doesn't collapse to one ambiguous number.
- **Visual: bars + table (chosen)** vs. ranked bars alone vs. table alone — the combination gives both the visual ranking and the exact, re-sortable figures; the table is where transaction counts and precise percentages live. Both are click-through to the underlying transactions.
- **Top-N: truncate with a hidden-count note (chosen, after review)** vs. an "Other (k payees)" fold (the first cut) vs. always-all — the owner found the "Other" row added clutter without insight (it's not a payee you can act on); plain truncation with an explicit "(58 hidden)" note keeps the chart clean and the omission honest, and `top_n = 0` still shows everything.
- **Aliases: canonical roll-up (chosen)** vs. raw `payee_id` — raw grouping would split one merchant across its alias spellings, defeating a "top payees" view.
- **Transfers: exclude + toggle (chosen)** vs. always-exclude — the toggle keeps parity with Income & Expense (ADR-064), where the owner explicitly wanted it.
- **Migration to widen the CHECK (required)** vs. reusing a reserved type — `payee` was never in the reserved list (unlike `income_expense`), so a migration is unavoidable; 0023 follows the 0014 recipe.
- **Ranked snapshot, no time bucketing (chosen)** vs. payee-over-time — E2 answers "who, over this period"; a payee trend line is a possible later report but would dilute the ranking view.

---

## Consequences

### Positive
- Fills the last common "by whom" cut of the spending data; complements Spending Over Time (by category) and Income & Expense (over time).
- Reuses the established machinery end-to-end — strict-outflow rule, `txn_category_line`, canonical roll-up, FX/exclude-no-rate policy, the ADR-039 window lifecycle — so behaviour is consistent and the new surface area is small (one migration, one repo method, one pure module, three UI files).
- Pure `payee_report.build_report` is fully unit-checkable; the aggregate's roll-up, exclusions, transfer toggle and FX exclusion were verified offscreen on a seeded DB.

### Negative / trade-offs
- **A payee can still be mis-grouped** if its aliases aren't linked to a canonical (ADR-029) — it then shows as separate rows (and as separate drill-downs). That's a payee-data hygiene task, not a report bug, but the report makes it visible.
- **Top-N truncates silently in the chart** — the omitted tail isn't drawn; only the summary's "(k hidden)" note flags it. Raising `top_n` (or setting it to 0) shows more. The grand total and each row's pct are over *all* payees, so the visible bars can sum to under 100% — intentional (each bar's % is its true share), but a user skimming only the bars sees a partial list.
- **Drill-down account scope is coarse** — the drill-down window models a single account or all accounts, not an arbitrary subset. When the report filters to *one* account the drill is exact; when it filters to a *subset*, the click opens the cross-account view, which can over-include that payee's spend from accounts outside the report's filter. The common all-accounts and single-account cases are exact.
- **The drill list isn't strict-outflow-filtered** — it shows *all* of that payee's transactions in range (including any refunds/inflows), not just the expense outflows the bar summed. That's deliberate ("show me the transactions"), but the drill footer total can differ from the bar.

### Ongoing responsibilities
- A future report type still means: a new migration to widen the CHECK, a filter dataclass + registration in `reports/filters.py`, and a `New Report…` / dispatch entry — the same checklist this ADR followed.
- If payee→category memorisation (Arc G) lands, the Payee report is the natural place to surface "this payee usually maps to category X".
