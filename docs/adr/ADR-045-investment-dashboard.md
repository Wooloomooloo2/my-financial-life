# ADR-045 — Investment dashboard (tabbed summary + value chart; analytics tabs)

**Date:** 2026-06-09
**Status:** Accepted (Phase 1 shipped, then amended same-day — see Amendment; Phase 2 planned)
**Related:** ADR-044 (holdings/prices — supplies `HoldingsView`, the data this screen renders), ADR-033/034 (the per-account summary this restructures), ADR-026 (hand-rolled paintEvent charts + `chart_helpers`; [[feedback-chart-engine-preference]]), ADR-018 (no-pie rule — the allocation tab uses a treemap, which keeps that rule intact), ADR-039 (the reusable `CheckListPanel` reused for security selection).

> **Amendment (2026-06-09) — value-over-time correction + historical prices.**
> On reviewing the shipped Phase 1, the owner clarified that the Overview top-left chart should be **portfolio valuation *over time*** (a time series of what the account has been worth), not the cost-basis-vs-market-value *snapshot* I'd put there. The snapshot bar widget (`value_chart.py`) was correct but mis-placed — it **moved to a new Positions tab**; the Overview now hosts a new **`value_history_chart.py`** (two lines, Invested + Market value, with a green/red fill between them). This required pulling the **historical-price** work forward from "round 3": `prices.TiingoClient.fetch_historical` + `backfill_historical_into` (one call per ticker → `security_price`), `Repository.price_series` / `get_security_price_nearest` / `bulk_upsert_security_prices`, and `holdings.compute_value_history` (FIFO replay snapshotting cost basis + market value at month-ends; cost fallback for any holding unpriced on a date, flagged `fully_priced=False`). A **Backfill history** button in Manage ▸ Securities populates it. Verified on live data: the Invested line matches FIFO cost-basis snapshots exactly; the full backfill→series→nearest→value-history chain works; all current holdings are tickered (100% of present cost basis), so the recent era is fully real.
>
> The **Portfolio** tab (renamed from Positions) defaults to an **allocation treemap** (`treemap_chart.py`, squarified — sized by market value, cost-basis fallback when nothing is priced yet, unpriced excluded with a footnote) with a **view switch** to the cost-vs-value bars. The treemap was always the chosen allocation viz; it's pulled forward from Phase 2 to be the default portfolio view (the owner's intent). Tabs are now **Overview / Holdings / Portfolio**. The body below describes the original Phase 1 shape; this amendment supersedes the Overview/value-chart and Positions parts of it. **Still Phase 2:** Returns + Dividends tabs, and a period-zoom selector on the value-over-time chart.

---

## Context

The investment per-account summary (ADR-044) worked but was cramped and cash-shaped: the Holdings table sat in a narrow bottom strip, and the top-left chart was the cash-in/out `BalanceFlowChart` — the wrong lens for a brokerage. The owner asked to rework it into a real dashboard: roomy holdings **with search**; a **value-driven** chart (cost basis vs market value, gain/loss); an **overall return** view (appreciation + dividends + realised); a **dividends/income** view; and a **portfolio allocation** breakdown. It's too much for one page.

**Decisions (confirmed):** **tabs** in the summary for investment accounts (cash accounts unchanged); **treemap** for allocation (the owner floated a pie — a treemap scales to ~30 holdings without slivers and isn't a pie, so the ADR-018 no-pie rule stands); **staged delivery** — Phase 1 ships the structure + the value chart; Phase 2 adds the analytics tabs. No historical prices are needed (the value chart is a current snapshot; a dividend timeline uses recorded dates) — value-over-time stays the round-3 (historical-prices) item.

---

## Decision

### Phase 1 (shipped)

**Tabbed restructure — `account_summary_window.py`.** For `family == "investment"` the central widget is a `QTabWidget` (the first tabs in the main UI). `__init__` branches: `_build_investment_tabs()` vs `_build_cash_layout()` (the original ADR-033/034 single page, factored out unchanged). `reload()` splits the same way — a shared prefix (account, status breakdown, scheduled/upcoming → the info panel) then `_reload_investment()` vs `_reload_cash()`, so the investment path never touches the cash-only chart/report/period/Top-N widgets (which aren't built for it).

- **Overview tab**: a splitter of the **ValueChart** (left) + the existing info card (right). The cash-flow `BalanceFlowChart` is not shown for investment accounts.
- **Holdings tab**: full-width — a live **search box** (substring over symbol + name) above the ADR-044 holdings table + the Account-value/Unrealised/Realised/Cash totals line. `_update_holdings_panel` now stores the view and `_render_holdings_table` re-renders through the search filter.

**Value chart — `value_chart.py`** (new paintEvent widget; models `spending_chart.py`'s segment paint + `_hitmap` hover, reuses `chart_helpers.nice_ticks`). One vertical bar per security, two-tone (owner's spec): full height = `max(cost, value)`; base `0..min` = cost-basis tone (blue-300); tip = **green** when value ≥ cost (gain) or **red** when value < cost (loss); an **unpriced** holding (value None) draws a single cost-basis bar so the chart is useful before prices are entered. Hover tooltip = security · cost · value · gain (±, %); currency-aware (formats with the account's symbol — `chart_helpers.fmt_currency` is GBP-hardcoded). **Selection** ("all / one / any number"): a **Securities…** button opens a `_SecurityPickerDialog` wrapping the reusable `CheckListPanel` (search + select-all); a **Portfolio total** checkbox collapses to one aggregate bar (total cost vs total priced value). `None` selection means "all" so the chart tracks holdings as they change.

### Phase 2 (planned)
- **Returns tab**: per-security + total **return = unrealised appreciation + realised gain + dividends/distributions** (table + return-per-security bar). Needs `compute_returns()` in `holdings.py` summing `CASH_IN_ACTIONS` cash per security alongside the FIFO replay.
- **Dividends tab**: income/cash generated — a dividends-over-time bar (recorded dates, no prices needed) + a per-security total, distinguishing dividends from cap-gain/interest distributions.
- **Allocation tab**: a **treemap** by market value (new `treemap_chart.py`, squarified paintEvent, `colour_for` palette, hover = security · value · %); unpriced excluded with a note.

---

## Consequences

### Positive
- The Holdings table finally has a full tab + search; the chart answers the brokerage question ("how is each position doing vs what I paid?") at a glance, with green/red tips and per-security or whole-portfolio framing.
- Cash accounts are untouched — the branch is clean, the original layout factored out verbatim.
- New chart reuses the established paintEvent toolkit and `CheckListPanel`, so it matches the app and adds no dependency. Verified on live data: gain bars have value > cost (green tip = value − cost), loss bars cost > value (red tip = cost − value), unpriced render cost-only; the portfolio-total toggle aggregates correctly.

### Negative / trade-offs
- **The value chart needs prices to show value** — before any prices it draws cost-basis-only bars (still useful for "what did I put where"), and the unpriced banner nudges toward Manage ▸ Securities.
- **A long portfolio is many bars.** The Securities… filter and Portfolio-total toggle manage this; bars sample x-labels when tight. A scroll/zoom is a later nicety if needed.
- **Tabs diverge investment from cash accounts.** Acceptable — a brokerage genuinely needs a different screen; the shared info panel + reload prefix keep the common parts in one place.

### Ongoing responsibilities
- Phase 2's analytics reuse the same `HoldingsView` + transaction rows; `compute_returns()`/dividend aggregation belong in `holdings.py` next to the FIFO engine, not the UI.
- The allocation **treemap is not a pie** — the ADR-018 no-pie rule remains in force; revisit only if a true pie is ever explicitly chosen.
- Value-over-time (a time-series value chart) still depends on historical prices (round 3); this round's value chart is deliberately a current snapshot.
