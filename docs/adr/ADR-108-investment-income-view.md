# ADR-108 — Investment Income view: per-security yield table + trailing-12-month income chart

**Date:** 2026-06-25
**Status:** Implemented (2026-06-25) — titled **Investment Income** / all income kinds; both yield bases; TTM default.
**Builds on:** ADR-046 (holdings / returns engine — `compute_returns`), ADR-044/045 (FIFO holdings + value history), ADR-089 (reinvested dividends as income + the *Include reinvested dividends* toggle), ADR-093 (bond/option multipliers + accrued interest), ADR-055 (multi-currency display, exclude-no-rate), ADR-082 (single-source `periods.py`), ADR-084 (`ReportFilterDialogBase`), ADR-083/034 (report drill-down → `TransactionsListWindow`), ADR-066 (ranked table + click-through pattern), ADR-105 (raise report above register after the modal filter).
**Relates to:** ADR-088 (Income Over Time report — distinct, see *Overlap*).

---

## Context

Dividend / income investing is a major FIRE use case, and MFL has no view built around it. The owner supplied a spreadsheet to model: a **per-security, holdings-style table focused on income** (yield + income received) plus a **small chart of income over the period** (usually a year). **Scope (owner-confirmed): all investment income** — cash dividends, reinvested DRIPs, bond coupons / interest, and cap-gain distributions (everything `is_income` classifies) — so the view is titled **Investment Income**, not strictly "Dividends".

The important discovery is that the engine already computes most of this. `holdings.py:compute_returns()` returns, per security, a `SecurityReturn` carrying `shares`, `cost_basis`, `market_value`, `unrealized`, `realized_window`, **`dividends_window`**, `total_return`, `priced`, plus a portfolio-level `ReturnsResult` with a dividends total. Income actions are already classified (`is_income` / `is_reinvest` in `qif_actions`), and reinvested DRIPs are already valued at `qty × price` (ADR-089). So the table is **~80% existing data** — the genuinely new work is the yield metrics, a monthly income aggregation for the chart, and the trailing-12-month default.

### Spreadsheet column → existing data

| Spreadsheet column | Source |
|---|---|
| Name | `SecurityReturn.name` |
| Ticker | `.symbol` |
| Currency | security / account currency (minor lookup) |
| Price | latest price (holdings view) |
| Shares | `.shares` |
| Cost basis | `.cost_basis` |
| Market value | `.market_value` |
| Value gain | `.unrealized` |
| Weight | `market_value ÷ Σ market_value` (derived) |
| **Net dividends received** | `.dividends_window` |
| **Net yield (YoC / YoM)** | 🆕 `dividends_window ÷ cost_basis` and `÷ market_value` |
| Total gain | `.total_return` |

## Options considered

1. **A "Dividends" tab on the Investment Returns window** vs **a standalone window.** → **Standalone** `DividendsWindow`, sibling of `InvestmentReturnsWindow`, reusing the same `compute_returns` call. Returns is about total-return / IRR; this is a full-width income/yield table + its own chart, a distinct task and audience. Bolting a tab on would crowd both.
2. **Extend Income Over Time (ADR-088)** vs **new view.** → **New view.** Income Over Time is a whole-portfolio *cashflow* income chart across all income categories over time; the Dividend view is *per-security yield/holdings*. Different grain, different question. Per the P3 de-overlap ethos (ADR-084), this is a distinct affordance, not a duplicate to consolidate.
3. **Yield basis: yield-on-cost / yield-on-market / both.** → **Both** (owner). Yield-on-cost (income vs what you paid) is the FIRE hero metric; yield-on-market (current yield) is the compare-to-the-market metric.
4. **Window: literal-YTD vs trailing-12-month.** → **TTM default** (owner), selectable across the standard presets. TTM is the existing investment-context **`1y`** (rolling-12-month) key in `periods.py` — no new period key required. TTM avoids the early-year YTD understatement and yields a true *annual* yield; YTD stays available as a preset.
5. **Reinvested dividends in / out.** → **In by default**, behind the ADR-089 *Include reinvested dividends* toggle for consistency. `dividends_window` already books reinvests at `qty × price`, so this is a presentation toggle, not new valuation.

## Decision

### 1. New `InvestmentIncomeWindow` — Investment ▸ Investment Income…

A standalone report `QMainWindow`, sibling of `InvestmentReturnsWindow`, added as `_investment_income_action` in the same menu group and dispatched the same way. Period + account scope come from the shared `ReportFilterDialogBase` (ADR-084) with `make_period_combo` / `periods.py` (ADR-082); **default period = `1y` (trailing 12 months)**. The window `raise_()`+`activateWindow()`s after the modal filter closes (ADR-105). The *Include reinvested dividends* checkbox (ADR-089) lives on this filter, defaulting on.

### 2. Engine — reuse `compute_returns` for the table; one new pure aggregator for the chart

- The per-row table is a `compute_returns(window = selected bounds, include_reinvested = toggle)` call → `SecurityReturn` rows. **No engine change** for the existing columns; the view surfaces **Price** (already in the holdings view) and **Currency** (security/account) alongside.
- Two derived yield columns, pure, in a new `reports/dividends.py`:
  - **Yield on cost** = `dividends_window ÷ cost_basis`
  - **Yield on market** = `dividends_window ÷ market_value` (`None` when unpriced)
  - Guard zero/short-history denominators. On the **TTM** default these are true annual yields (12 months of dividends ÷ current basis/value). For a **non-12-month** window the column is **period-literal, not annualised**, and the header is suffixed (e.g. *Yield (period)*) so a partial window can't read as a misleading annual figure. *(Open question — see below.)*
- **Monthly income series** for the chart: new pure `income_by_period(txns, bounds, granularity="month", include_reinvested=True, fx=…)` → `[(bucket_key, Decimal)]`. Sums all cash income (`is_income` — dividends, coupons/interest, cap-gain distributions) + optionally reinvested DRIPs valued `qty × price` (ADR-089), converted to **base currency** (ADR-055). Buckets are enumerated via `periods.py` with the same `strftime` keying SQL uses (the ADR-064 lesson — avoid a New-Year `%W` straddle bug). A dedicated bucketer is cleaner than diffing the engine's cumulative `ReturnPoint.dividends_cum` and avoids edge artifacts.

### 3. Chart

A small **bar chart** (hand-rolled `paintEvent`, reusing the `income_expense_chart` / `BalanceFlowChart` patterns and theme-aware `chart_helpers.chart_accent()`): one bar per month over the period (12 for TTM), y = income in base currency, optional dashed average line. Bars are **click-through** to that month's income transactions via the shared `TransactionsListWindow` (`for_kind`(income) + month bounds, ADR-034/083) — reuses existing drill plumbing. *(Drill may land as a fast-follow if it risks the v1 timeline.)*

### 4. Multi-currency

Per-row **Currency** with native-currency price / market value (matching the spreadsheet's mixed USD/GBP rows). Portfolio **Weight**, the totals row, and the **chart** are in **base currency** via the same ADR-055 FX path `compute_returns` already uses (no-rate slices excluded + bannered, never par-added). A no-rate security still shows native row values but drops out of the base-currency weight/total/chart with the standard missing-rate banner.

### 5. Summary strip (the FIRE headline)

Above the table: **total income (TTM)**, **portfolio yield on cost**, **portfolio yield on market**, and **projected forward annual income** — the latter = TTM income used as a run-rate proxy, **explicitly labelled "trailing-12-month basis"** (not a forecast; no forward dividend/coupon declarations are modelled). Projection is *the* FIRE number and falls out of the TTM window for free.

### Columns (final, sortable per ADR-066)

Name · Ticker · Currency · Price · Shares · Cost basis · Market value · Value gain · Weight · Yield on cost · Yield on market · Net income received (TTM) · Total gain.

## Scope / consequences

- **No migration** — read-only over existing `txn` / `security` / `price` data.
- **No change to `compute_returns` semantics** — additive new view; the Investment Returns report is untouched.
- **Reuses:** `ReportFilterDialogBase`, `periods.py`, `chart_helpers`, `TransactionsListWindow` drill, the ADR-089 reinvest toggle + valuation, the ADR-055 FX/exclude-no-rate path.
- **Net-new files:** `reports/investment_income.py` (pure — yields + `income_by_period`), `ui/investment_income_window.py`, `ui/investment_income_chart.py`; menu action + dispatch wiring in `register_window.py`.
- **Distinct from Income Over Time (ADR-088)** — confirmed not a duplicate (per-security yield vs whole-portfolio cashflow income).
- **Public demo:** the existing `mfl_public.mfl` already carries cash dividends + DRIP history (US brokerage / ISA) plus a bond coupon, so the view populates without regenerating; we may add a couple more dividend payers so the monthly chart reads well.

## Resolved (owner)

- **Scope & title** — **include all income; title "Investment Income".** Counts cash dividends, reinvested DRIPs, bond coupons / interest, and cap-gain distributions (everything `is_income` classifies). The spreadsheet said "dividends", but the FIRE framing is total passive income.

## Resolved at build

- **Non-TTM yield labelling** — **period-literal** (income ÷ cost over the window, *not* annualised); when the window isn't the 12-month default a summary note points the user at *Projected annual income* for the annualised figure. The headline projection annualises by the window's day-fraction (`income × 365 ÷ days`), so it is correct for any period while the 1y default ≈ the income received.
- **Bar drill-through** — **shipped in v1**: double-clicking a security row opens its transactions over the window via the shared `TransactionsListWindow` (`for_security`), matching the Investment Returns drill. (A per-month bar drill would be the next fast-follow.)

## Implementation notes (as built, 2026-06-25)

- **Net-new files:** `reports/investment_income.py` (pure — `IncomeFilters`, `income_for_txn`, `income_by_security`, `income_by_month`, `enumerate_months`), `ui/investment_income_chart.py` (`IncomeBarChart` paintEvent), `ui/investment_income_filter_dialog.py` (`InvestmentIncomeFilterDialog` over `ReportFilterDialogBase`), `ui/investment_income_window.py` (`InvestmentIncomeWindow`). Wiring in `register_window.py`: import, `_investment_income_action` (Reports menu, after Investment Returns), singleton `_on_investment_income_report`, `_investment_income_win` reference.
- **Engine reuse, not change:** the window runs the same per-currency `compute_returns` replay + FX merge as `InvestmentReturnsWindow` for cost / market value / unrealized / realized / shares; the income column, yields and chart read the pure aggregator. Units match the holdings engine (major-currency floats; `holdings._to_money` doesn't rescale), so a security's income equals `compute_returns.dividends_window` when reinvests are included.
- **Total gain** is defined as *value gain + realized + income-received* (the same income the toggle controls), so the row's columns add up; with reinvests included this equals `compute_returns.total_return`.
- **Columns (as built, r2 income-first):** Symbol · Security · Ccy · **Income** (bold) · Yield/cost · Yield/mkt · Price · Shares · Cost · Market value · Value gain · Weight · Total gain. Income + the two yields lead — right after the security identity — because this is an *income* view; the holdings / price / total-return columns follow, with Total gain (the largest figure) last. The first cut put Income at column 11, which read as buried and was easily confused with the bigger Total gain column (owner feedback); the income cell is now bold so it's unmistakably the focus. Price + Ccy are native/informational; all monetary aggregates are in the display currency (native when uniform, else the first account's — a note flags conversions), consistent with the Investment Returns report.
- **Chart (r2):** each non-zero monthly bar is labelled with its amount (income is usually quarterly, so only a few months carry a label), answering "what did each month pay?" directly.
- **Verified offscreen on `mfl_public.mfl`:** window builds + populates (7 holdings); income reconciles (VWRL £744.08 + MSFT £28.82 + AAPL £26.29 = £799.19 display / £814.26 native pre-FX); per-row yields = income ÷ cost / ÷ market; weights sum to ~100%; chart renders 13 monthly bars; filter dialog defaults to `1y` + reinvest-on and round-trips. **Aggregator unit-tested** with synthetic rows: `IntInc` (coupon) + `CGShort` (cap-gain) counted (all-income scope), no-cash reinvest valued `qty × price`, toggle 111→79, out-of-window excluded. Plus `compileall` (all modules), import-all smoke (0 failures), IRI guard 6/6.
- **Public demo refreshed (same day)** so the toggle and bond yield are visible in the real app: `tools/make_public_demo.py` now books a **quarterly DRIP** on the Workplace Pension's L&G Global fund (`ReinvDiv`, no cash leg, valued at qty × price) and **semi-annual bond coupons** on the Apple 4.2% 2032 (`IntInc`, $105 each). Regenerated `mfl_public.mfl` (636 txns). In the view: the bond now shows income £163.55 at **4.22% yield-on-cost** (= the coupon rate, bought near par); total income **£1,649.40 with reinvested included → £982.28 excluded** (Δ £667.12 = the DRIP), so the *Include reinvested dividends* toggle has a visible effect.
