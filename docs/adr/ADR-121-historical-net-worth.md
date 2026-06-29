# ADR-121 — Historical net worth (net worth over time)

**Date:** 2026-06-29
**Status:** Accepted (R1)
**Related:** ADR-067 / ADR-120 (the point-in-time Net Worth screen this extends). ADR-044 (investment market value = cash + Σ shares × price). ADR-046 / ADR-093 (`compute_value_history`, FIFO replay, bond/option multipliers). ADR-055 (FX: convert before summing, exclude no-rate accounts — never par-add). ADR-082 (period vocabulary). ADR-026 (hand-rolled paintEvent charts). ADR-075 (`gather_*` Qt-free-but-repo-coupled gather pattern).

## Context

The Net Worth screen (ADR-067/120) is **point-in-time only** — it answers "what am I worth *today*" but not "how has it moved." Tracking the trajectory of assets, debts, and net worth over time is the missing other axis on the balance sheet, and the last open item of the investment arc (memory: "R4 … historical net worth open").

The exploration confirmed every primitive needed already exists and the project's **computed-not-stored** philosophy applies cleanly:

- `Repository.balance_as_of(account_id, date)` — cash balance at a date (`opening + Σ amount ≤ date`).
- `holdings.compute_value_history(txns, sample_dates, price_series, multipliers)` — investment **holdings** market value at each sample date in a single FIFO pass (nearest-prior price via bisect, bond/option multipliers, falls back to cost when unpriced).
- `Repository.convert_amount(amount, from_ccy, to_ccy, on_date)` / `get_fx_rate_nearest` — FX at an **arbitrary historical date** with a 6-step nearest-prior fallback chain.

There is no stored balance-snapshot table, and we don't add one — the series recomputes from the ledger + price history + FX history, same as every other figure here.

### Decisions taken with the owner (two forks)

- **Placement:** a **"Now | Over time" view toggle on the existing Net Worth window** (reusing its display-currency selector + show-closed toggle), *not* a separate saved report. Keeps all of "net worth" in one screen / one mental model; no saved-report plumbing.
- **Chart shape:** a **stacked area + net line** — asset families stacked above zero, debt families below zero, with a bold net-worth line on top. Richest of the options (composition *and* the bottom line over time), and it honours the no-pies rule (ADR-018) since it's an area, not a pie. (Rejected: a single net-worth line — loses composition; three Assets/Debts/Net lines — no per-family detail.)

## Decision

**The unifying idea:** an account's total value at a date = **cash balance** (transaction replay, inclusive `≤ date` per ADR-040) **+ holdings market value** (`compute_value_history`; zero for non-investment accounts, which have no security txns). So one code path values *every* family — cash, credit, loan, property, vehicle, and investment — without branching per family at the call site.

**New pure module `mfl_desktop/net_worth_history.py`:**
- `month_end_samples(start, end)` — `[start]` + each interior month-end + `[end]` (mirrors the Returns report's sampling so the two investment views line up).
- `cash_balance_at_dates(txns, opening, samples_iso)` — one ascending pass returning the cash balance at each sample (the multi-date generalisation of `balance_as_of`).
- `gather_net_worth_history(repo, *, sample_dates, display_ccy, family_kinds, include_closed)` — Qt-free but repo-coupled (the ADR-075 `gather_*` pattern): per account, replay cash + add holdings value, **FX-convert at each sample date** (ADR-055 — exclude an account at any date it can't convert rather than par-adding), and bucket into per-date asset/debt families. Returns a `NetWorthHistory` (a list of `NetWorthPoint{date, family_assets, family_debts, asset_total, debt_total, net}` plus `excluded_any` / `fallback_used` flags). FX rates are memoised per `(currency, date)` so the 6-step lookup runs once per pair, not once per account.
- The window passes the family→kind classification (`family_kinds`) in from its existing `_FAMILY_VIEW`, so the asset/debt split stays single-sourced and the pure module imports no Qt.

**New chart `mfl_desktop/ui/net_worth_history_chart.py`** — a hand-rolled paintEvent (ADR-026), modelled on `returns_chart.py`: stacked filled areas for asset families above the zero line, debt families below, the zero axis drawn, and a bold net-worth polyline on top; `nice_ticks` / `fmt_currency` / family colours from `chart_helpers` and the screen's `_FAMILY_VIEW` so colours match the donut. Hover shows the date's net worth.

**Net Worth window wiring:** a **Now | Over time** pill toggle (same control as the Assets|Debts toggle) over a `QStackedWidget` — page 0 the existing donut + columns, page 1 the history chart with a small period selector (Last 12 months / 3y / 5y / All, ADR-082 vocabulary; default 1y, monthly sampling). The display-currency selector and show-closed toggle drive both pages. The missing-rate banner is reused (the history view notes when accounts were excluded at some dates).

## Consequences

- The balance sheet gains its time axis without a schema change, a migration, or stored snapshots — it recomputes from the ledger + price + FX history, consistent with ADR-044/046/055.
- **Property / vehicle valuation is transaction-driven (works correctly), not market-fed.** These families have no automatic price feed (unlike securities), but the series values every account by its running balance (`opening + Σ amount ≤ date`), so **revaluation entries posted inside the asset account are tracked over time** — the owner's Banktivity workflow (a dated appreciation/depreciation transaction, plus selling-cost entries at disposal). A house bought, revalued up over the years, and sold rises and falls correctly (verified: 0 → 300k → 350k → 400k → 0). The only "flat" case is an asset the user simply never revalues — that's a data-entry choice, not an engine limitation. A dedicated valuation-timeline UI is therefore **not needed** (it would duplicate the transaction mechanism).
- **Known v1 limitations** (documented, deferred): FX/price coverage that doesn't reach far enough back makes early points exclude some accounts (surfaced via the existing banner) — the series is only as deep as the data. A clicked point does **not** yet drill into that date's composition (future round).
- Cost: ~one `compute_value_history` FIFO pass per investment account + one cash replay per account + memoised FX per `(ccy, date)` — ~100 ms for a typical file; recomputed on open and on currency/period/closed-toggle change.
- Pure compute (`net_worth_history.py`) is unit-testable headless; the chart + toggle are verified offscreen on `mfl_public.mfl`.

### Deferred to later rounds
- Click-a-point drill-down to that date's composition (or to the underlying accounts).
- A saved-report variant (if the owner later wants persisted period/account scoping in the sidebar).

(A property/vehicle valuation-timeline UI was considered and **dropped** — the owner revalues via transactions inside the asset account, which the running-balance valuation already tracks, so a separate feature would be redundant.)
