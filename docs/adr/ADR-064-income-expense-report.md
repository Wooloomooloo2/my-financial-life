# ADR-064 — Income & Expense report (cash-flow over time)

**Date:** 2026-06-14
**Status:** Accepted
**Related:** ADR-039 (saved-reports framework — type enum, filter dataclass registry, sidebar section, Save/Save As; this fills the reserved `income_expense` slot). ADR-018 (reports framework + strict-outflow / no-pies rules; this report uses bars + a line, no pie). ADR-026 (paintEvent chart engine). ADR-033/034 (`BalanceFlowChart` — the income-up/expense-down + line shape this borrows). ADR-056 (Sankey — the kind-based income/expense definition + the display-currency pass this mirrors). ADR-055 (Net Worth display-currency policy — exclude no-rate, never par-add). ADR-051 (`txn_category_line` split-unrolled view this aggregates over). First round (**E1**) of the **Reports arc (Arc E)** from the owner's 8-arc braindump.

---

## Context

The saved-reports framework (ADR-039) reserved an `income_expense` type in the `report.type` CHECK and showed it as "(coming later)" in the New Report dialog, but never built it. Arc E's brief lists "income & expense" as the first reports thread. It's the classic personal-finance **cash-flow view**: how much came in vs. went out, per period, over time — the report the owner is most likely to use day-to-day, and the natural complement to the existing *Spending Over Time* (expense-only) and *Sankey* (single-period flow) reports.

Four scoping questions were put to the owner via `AskUserQuestion`:

1. **What counts as income vs. expense?** → **By category kind** — income = inflows (`amount > 0`) on `kind='income'` categories, expense = outflows (`amount < 0`) on `kind='expense'` categories, **transfers excluded**. Same rule as the Sankey report and the Budget. (Rejected: raw amount sign, which works without category kinds set but lets refunds/reversals/transfers muddy the totals. The owner's data already has kinds largely set — 39 income / 155 expense / 14 transfer.)
2. **Chart shape?** → **Income bars up / expense bars down + a net line** — the `BalanceFlowChart` vocabulary from the per-account summary, already proven. (Rejected: grouped side-by-side bars; net-only surplus/deficit bars.)
3. **Multi-currency?** → **A display-currency selector**, converting each amount before summing (this report sums across accounts, so a bare-pence par-add would re-introduce the ADR-055 bug). (Rejected: defer to single-currency.)
4. **Summary panel?** → **all four** — total income & expense, net saved, savings rate %, and average income & expense per period (with the averages also drawn as reference lines on the chart).

`income_expense` is already in the `report.type` CHECK (added in migration 0014), so **no migration** is needed.

---

## Decision

Ship **Income & Expense** as a saved report type, following the established add-a-report-type path:

- **Filter dataclass** `IncomeExpenseFilters` (`reports/filters.py`): `period_key` (default `"1y"` — the trailing-12-months cash-flow horizon, matching Arc A's register default), optional custom range, `granularity` (`"auto"` + weekly/monthly/quarterly/annually), `account_ids` (empty = all), and `include_transfers` (default `False`). Registered in `_FILTER_CLASSES`; made selectable in `NewReportDialog._AVAILABLE_TYPES`. The **display currency is deliberately NOT persisted** — like Net Worth (ADR-055) and Sankey (ADR-056) it's a view preference re-resolved (base currency → GBP → first in use) each open.
- **Transfers excluded by default, with a toggle.** `kind='transfer'` categories are inherently neither income nor expense, so the kind rule already drops them. But a transfer *pair leg* can carry an income/expense-kind category (e.g. an imported inter-account move filed under an account-name expense category, or a leg not yet re-categorised on linking), which would otherwise inflate the totals. So `income_expense_series` takes `include_transfers` (default `False`): when off it additionally filters `transfer_id IS NULL`, dropping any line the app has linked as a transfer pair regardless of its category. The filter dialog surfaces an **"Include transfers"** checkbox (off by default); the summary panel shows "Transfers: excluded / included". **Limitation:** a transfer recorded under an income/expense category and *not* linked as a pair has no `transfer_id` and is indistinguishable from a real flow here — the fix for that is to set its category's kind to `transfer` (or reconcile it as a transfer pair), not a report toggle.
- **Repository aggregation** `income_expense_series(date_from, date_to, granularity, account_ids, display_currency)`: one SQL pass over the `txn_category_line` view joined to `category` (kind) + `account` (currency), `GROUP BY bucket, kind, currency`, with the kind-based income/expense `CASE`. Each (bucket, currency) total is FX-converted to the display currency at the period-end date via `convert_amount`; **no-rate buckets are excluded and collected under `unconverted`** (ADR-055 policy — never 1:1). Returns `{income: {bucket: pence}, expense: {bucket: pence}, unconverted: {ccy: pence}}` keyed by the `strftime` bucket string.
- **Pure core** `reports/income_expense.py` (no Qt, no SQL): `enumerate_buckets(date_from, date_to, mode)` builds the full continuous bucket list (so empty periods still appear on the x-axis) with keys **byte-identical to the SQL `strftime` output** — calendar-analytic for year/quarter/month, day-walked for weeks (SQLite's `%W` can split a Mon–Sun span across two `%Y` years at New Year, so we read real-date keys to stay identical to the aggregate). `build_buckets` zips the order against the pence maps (gap-fill zero, pence→pounds). `compute_summary` derives totals / net / savings rate / per-bucket averages. All dates injected → verifiable offscreen.
- **Chart** `ui/income_expense_chart.py`: a paintEvent widget modelled on `BalanceFlowChart` but with a **single shared y-axis** (income, expense and net are all the same currency at comparable magnitudes — `|net| ≤ max(income, expense)` — so one `nice_ticks` scale fits all three, no dual axis needed). Income bars up (emerald), expense bars down (red), net line (blue-600), dashed average-income / average-expense reference lines, hover tooltip, legend.
- **Window** `ui/income_expense_window.py`: non-modal `QMainWindow` with a top bar (name / **Display in:** selector / Filter… / Save / Save As…), the chart left, a summary panel right (period, account filter, income/expense/net/savings-rate/averages, and an excluded-currency note). `open_bare` + `load_from_id` constructors and the Save/Save-As/dirty/close-prompt machinery mirror `SpendingReportWindow`. **No drill-down** in E1 (the report has no per-category breakdown yet — that's the E3 category↔payee thread).
- **Filter dialog** `ui/income_expense_filter_dialog.py`: period preset + custom range + granularity + an accounts `CheckListPanel`. No category/kind/rollup controls (kind is the fixed aggregation rule).
- **Wiring** (`register_window.py`): a Reports-menu "Income & Expense…" item; dispatch in `_open_bare_report` / `_open_saved_report`; singleton-per-type (bare) and singleton-per-id (saved), same as the other reports.

---

## Options considered

- **Kind-based income/expense (chosen)** vs. raw sign — see Context Q1. Kind-based is consistent with Sankey + Budget and excludes transfers cleanly; its dependency on category kinds being set is largely satisfied on the owner's data, and an uncategorised-income gap is a data fix, not a report bug.
- **Income-up/expense-down + net line (chosen)** vs. grouped bars vs. net-only — owner pick; reuses a proven widget shape and keeps the income/expense magnitudes directly readable while the net line tells the surplus/deficit story.
- **Display-currency selector (chosen)** vs. bare pence — a cross-account totals report must not par-add currencies (the exact ADR-055 bug). Conversion lives in the Repository like Sankey; the selector is a view preference, not saved state.
- **Single shared y-axis (chosen)** vs. the dual axis `BalanceFlowChart` uses — the net line is bounded by the bar magnitudes here (unlike a cumulative balance line), so a second axis would add complexity for no readability gain.
- **No drill-down in E1 (chosen)** vs. category drill now — the income/expense split has no category dimension yet; drill belongs with the E3 category↔payee round. Keeping E1 focused ships the cash-flow view sooner.
- **No migration (chosen)** — the `income_expense` enum value was already in the CHECK (0014), so the type just needed a filter dataclass + window.

---

## Consequences

### Positive
- The cash-flow report the owner most wanted, in the saved-reports framework (Save / folders / sidebar) for free.
- Correct across GBP + USD via the same exclude-no-rate policy as Net Worth / Sankey — no silent par-add.
- Pure core is fully unit-tested offscreen, including the SQLite `%W` New-Year edge that would otherwise drop or misplace a week bucket.
- Establishes the reusable shape for the rest of Arc E (the payee report E2 can reuse the bucketing + display-currency plumbing).

### Negative / trade-offs
- **Depends on category kinds being set.** Income recorded under an expense-kind (or Uncategorised, which defaults to expense) won't count as income — a chart note isn't shown in E1 (the Sankey report carries that guidance); if it bites, add the same "set kinds" hint.
- **Conversion at period-end, not per-bucket-date.** A multi-year window with genuinely moving rates would slightly distort early buckets. Consistent with Sankey and harmless under the owner's sparse manual rates (`get_fx_rate_nearest` resolves the same rate at any date); revisit if real daily-rate history lands.
- **No category breakdown / drill** yet — deferred to E3.

### Ongoing responsibilities
- **Adding a new bucket granularity** means updating both `Repository._BUCKET_EXPR` and `income_expense.enumerate_buckets` in lockstep, or enumerated keys stop matching aggregated keys (the chart would show empty buckets next to real data).
- **The display currency must stay out of the saved filter blob** — it's resolved per open; persisting it would freeze a view preference into the saved report.
