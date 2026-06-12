# ADR-046 — Investment Returns report (total return: cost / unrealized / realized / dividends)

**Date:** 2026-06-09
**Status:** Accepted
**Related:** ADR-045 (investment dashboard — **supersedes its Phase 2 Returns + Dividends *tabs*** plan), ADR-044 (FIFO holdings engine — supplies the lot machinery `compute_returns` reuses), ADR-043 (investment txn columns + `qif_actions` action classification), ADR-039 (saved-reports framework — type enum, `filters.py`, `SpendingReportWindow` lifecycle, `CheckListPanel`, `SaveReportAsDialog`), ADR-026 (hand-rolled paintEvent charts + `chart_helpers`; [[feedback-chart-engine-preference]]), ADR-018 (no-pie rule — preserved), ADR-035 (`convert_amount` for the mixed-currency case).

---

## Context

ADR-045 Phase 2 planned two **per-account dashboard tabs** — Returns and Dividends. On review the owner reframed the work: he wants to *see total portfolio return with unrealized gains and dividends separated out but also combined per security*, both visually and numerically, filterable by security and time period, and — crucially — across **one account or the whole investment portfolio in one view**. A per-account tab can't do the cross-account part, and building two tabs *plus* a future cross-account view would mean two implementations of the same return math.

So this lands as a **saved report** instead (it fits the ADR-039 framework: cross-account, filterable, save/load, lives in the sidebar Reports section). It **replaces** the planned per-account Returns/Dividends tabs — set the report's Account filter to a single account for the per-account view, or leave it unset for the whole portfolio. The dashboard stays Overview / Holdings / Portfolio.

**Decisions confirmed with the owner:**
- **Chart = stacked composition** (absolute currency), drawn *relative to the cost line*: cost basis at the base, capital appreciation above it (green gain / red underwater), then period realized gains, then period dividends. The literal "broken down into cost, returns, dividends over time" the owner described.
- **Total return = unrealized appreciation + realized gains + dividends/income** (the full picture).
- **Realized gains and dividends are period-scoped** — they count only when the sale / distribution falls *inside* the selected window. A position sold years ago contributes nothing to a YTD view; a 3-year window containing the sale shows it. Accumulators reset to zero at the window's left edge.
- **Unrealized = lifetime, as-of-today** — the full unrealized gain of currently-held positions, regardless of when it accrued (consistent with the absolute-value chart; the period scoping applies only to the realized/dividend *flows*).

---

## Decision

### Compute engine — `holdings.compute_returns(...)` (pure Python, no Qt/SQL)
A single FIFO replay over **one account's** full transaction history (reusing `_Lot`, `_lot_cost`, and the `qif_actions` classifiers) that produces the chart series, an end-of-window per-security breakdown, and portfolio totals (`ReturnPoint` / `SecurityReturn` / `ReturnsResult`). The replay always processes the entire history so cost basis and open shares are correct, but a sell's realized gain and an income row only count toward the window accumulators when the transaction is dated `>= window_start`. New `qif_actions` helpers: `is_income` (wraps `CASH_IN_ACTIONS` — dividends/interest/cap-gain distributions) and `is_reinvest` (`REINVEST_ACTIONS`). A **reinvested distribution** (`ReinvDiv` etc.: a share-in with zero cash) is counted as income at its reinvested value (price × qty) — verified against the real export, which represents dividends as *either* a cash `Div` row *or* a `ReinvDiv` row but never both for the same event, so there's no double count. Market value uses the nearest-prior price per sample (cost fallback → `fully_priced=False`); portfolio totals count priced positions only (matching `compute_holdings_view`).

### Report plumbing — ADR-039 framework
- **`report.type`** gains `investment_returns`. The 0010 CHECK hard-lists the allowed types and SQLite can't ALTER a CHECK, so **migration 0014** recreates `report` with the widened list (same table-rebuild approach as ADR-032's `account.type` widening in 0008).
- **`InvestmentReturnsFilters`** in `reports/filters.py` (`period_key` / `custom_start` / `custom_end` / `account_ids` / `security_ids`; empty id-tuples = "all", the spending convention) with the standard JSON round-trip + dispatch registration. New `INVESTMENT_RETURNS_PERIOD_KEYS = (ytd, 1y, 3y, 5y, max, custom)` — investment-native, adding **`max`** (lifetime, first txn → today) which the spending presets lack.
- **`Repository.list_investment_accounts()`** + **`list_securities_for_accounts(account_ids)`** feed the filter checklists.

### UI
- **`returns_chart.py`** — paintEvent stacked-composition chart modeled on `value_history_chart.py` (reuses `nice_ticks`, the axis/hover/legend scaffolding). Per-segment area fills relative to the cost line: blue intact capital `0..min(cost,value)`; green/red appreciation `min..max`; teal realized above value (red downward for a net realized loss); gold dividends on top. Currency-aware; the "early periods use cost" note when any point falls back. Hover shows cost / market value / unrealized ±% / realized / dividends / total return.
- **`investment_returns_window.py`** — `QMainWindow` mirroring `SpendingReportWindow`'s top-bar / Save / Save As / dirty / close-prompt / `open_bare` / `load_from_id` / `reports_changed` scaffolding (no drill-down). Layout: a vertical splitter of the chart over a **per-security breakdown table** (Symbol / Security / Cost / Market value / Unrealized ± % / Realized / Dividends / Total return ± %) on the left, and a summary panel (period bounds, account/security filter summary, and the portfolio totals with a big Total return) on the right.
- **`investment_returns_filter_dialog.py`** — period preset + Custom dates, plus Accounts and Securities `CheckListPanel`s. The Securities list re-queries when the account selection changes (so it only offers securities held in the chosen accounts), preserving the surviving checked subset.
- **Wiring**: `new_report_dialog` enables the type; `register_window` adds a **Reports → Investment Returns** menu entry and bare/saved open branches (the report-window dicts widen to `QMainWindow`).

### Currency
When all selected accounts share a currency the report aggregates natively; a mixed-currency selection converts each account into the first account's currency via `Repository.convert_amount` (nearest-prior FX per sample date), with a note when a rate was missing or a fallback was used. The owner's portfolio is single-currency USD, so native is the live path; conversion is correctness insurance.

---

## Consequences

### Positive
- One report covers per-account *and* whole-portfolio returns with no duplicated math; the Account filter is the only difference. The dashboard stays focused on Overview / Holdings / Portfolio.
- **Verified on the live E*Trade export:** lifetime (`max`) realized = **$2,672.14**, matching `compute_holdings_view`; the fully-closed **TSLA +$354.00** shows in `max` but is correctly **absent from YTD** (sold pre-2026); per-year realized sums to the lifetime figure (2026 alone is +$13,067.63 because 2022–24 were realized *losses* — so YTD realized legitimately exceeds lifetime); per-security rows reconcile to the portfolio totals; lifetime dividends = $35,239.16 with **zero** security-dates carrying both a cash `Div` and a `ReinvDiv` (no double count).
- The chart's cost-relative stacking makes "how much of this is capital I put in vs gains I've made vs income I've taken" legible at a glance, and survives loss periods (red underwater notch).

### Negative / trade-offs
- **Period-scoped flows vs lifetime unrealized is a deliberate mix.** A YTD view can show a large unrealized gain that mostly accrued earlier — by design (it's a current snapshot, not a time-weighted period return). The chart's absolute-value framing makes this honest; a true time-weighted "period return %" is out of scope.
- **Mixed-currency aggregation is per-sample FX conversion**, not a rigorous multi-currency performance model. Adequate for the single-currency reality; flagged when a rate is missing.
- **Stock splits remain deferred** (ADR-044) — a split holding's shares/basis stay flagged-approximate upstream; the report inherits that.

### Ongoing responsibilities
- The return math lives in `holdings.py` next to the FIFO engine, not the UI — any future per-account embed or export reuses `compute_returns`.
- The no-pie rule (ADR-018) stands; this report adds no pie.
- Historical *net worth* (as-of a past date using that date's prices) is still not a thing — `compute_account_values` is a today snapshot. This report's chart uses historical prices correctly for the per-account/portfolio value line, but the net-worth headline is unchanged.

---

## Amendment (2026-06-09) — cost of shares sold + return on cost (ROI)

**Why.** For a position that's been (partly) sold, the report's "Cost" column showed only the cost basis of shares *still held*, so a fully-liquidated holding read **$0.00** — the owner saw VWID (sold in full on 2026-04-17) show Cost $0 / Market value — / Unrealized — despite a +$19,443.11 total return, and asked "where's the cost basis? I want return on cost." Cost basis of currently-held shares is the right *snapshot* figure, but it doesn't tell you the capital that produced a realized return.

**Change.** The FIFO replay in `holdings.compute_returns` already computes `cost_removed` per in-window sell (it's the cost side of `realized = proceeds − cost_removed`); it now **accumulates** that into a new `cost_basis_sold` on `SecurityReturn` and `ReturnsResult` (period-scoped, same as realized/dividends). The report defines **Cost (deployed) = cost of shares still held + cost of shares sold in the window** — the capital that generated the row's return — and adds a **Return % (ROI) = total return ÷ cost deployed**:
- Per-security table: the **Cost** column now shows deployed cost (held + sold), and a new **Return %** column shows ROI. Unrealized's own `%` still divides by *held* cost (gain on what you hold); the Total-return `%` moved into the dedicated Return % column.
- Summary panel: "Cost basis" becomes "**Cost (held + sold)**" when any shares were sold, and a "**Return on cost: ±X%**" line sits under the big total-return figure.

**Verified on the live data:** VWID (fully sold) now shows **Cost $31,533.15** (= $44,600.78 proceeds − $13,067.63 realized), Return % **+61.7%**; the whole-portfolio totals read **Cost (held + sold) $297,459.60 / Return on cost +12.7%** on **+$37,911.30** total return; held-only positions (DIVO +15.1%, SCHD +11.3%) are unaffected since they have no sold cost.

**Trade-off / still open.** ROI-on-cost treats **reinvested distributions as deployed capital** (the standard cost-basis view) — it isn't a time-weighted or money-weighted return, so it slightly overstates "deployed" by the reinvested amounts. **IRR (money-weighted return)** is the rigorous companion the owner also asked for; it's deferred to its own pass because it needs dated cash-flow plumbing (signed `txn.amount` per security + a terminal market value, and an *opening* market value at the window start for non-`max` periods) plus a root-finder and no-sign-change handling. Tracked as the next increment on this report.

---

## Amendment (2026-06-12) — money-weighted return (IRR / XIRR)

**Why.** The deferred companion above. ROI-on-cost ignores *timing* — $10k held for ten years and $10k held for one month both read as "cost deployed", so a strong recent year inflates the lifetime ROI and a YTD view's headline total return is dominated by lifetime unrealized gain that mostly accrued in earlier years (the documented ADR-046 period-mix). **IRR** answers the honest period question: given when and how much money actually went in and came out, what annualized rate reconciles the opening value to the closing value?

**Method (the cash-flow boundary = capital deployed into securities).** `holdings.compute_returns` now emits, alongside the existing window results, the dated **external cash flows** of the security portfolio, plus the **opening** and **terminal** market values that bracket them. The caller (`investment_returns_window`) converts each per-currency flow to the display currency at its own date, prepends `−opening_market_value` at the window start and appends `+terminal_market_value` at the window end, and solves a new pure-math **`holdings.xirr(flows)`** (bisection over `[−0.999999, 1000]`; robust where Newton diverges near `r = −1`; `None` on fewer than two flows / no sign change / single date). Sign convention: contributions/buys negative, sells/dividends/terminal positive.

Flow rules, derived from the FIFO replay already in place:
- **Cash buy** → `txn.amount` (negative — money out). **Sell** → `txn.amount` (positive — proceeds in). **Cash distribution** (Div/IntInc/CG) → `txn.amount` (positive). These reuse the *signed cash impact* the engine already trusts (it includes commission, ADR-043).
- **Reinvested distribution** (`ReinvDiv`) → **no external flow** — the cash never leaves the account, so for a money-weighted return it nets to zero (the acquired shares show up in the terminal value instead). It's still counted as *income* in `dividends_window`, unchanged.
- **In-kind share transfer** (`ShrsIn`/`ShrsOut`, ADR-053) → a flow at **market value on the transfer date** (`ShrsIn` negative = value in, `ShrsOut` positive = value out; cost-basis fallback when the date is unpriced, flagging `irr_fully_priced=False`). For a whole-portfolio view the matched legs cancel (same security, date, value); for a single account the value correctly leaves/enters.
- **Whole-account cash transfers** (`XIn`/`XOut`) → ignored, consistent with the report ignoring cash: capital isn't "deployed" until a buy.

**Opening-value boundary (the subtle part).** The opening market value is snapshotted from the FIFO lots *just before the first transaction dated on/after `window_start`* — the `(posted_date, id)` sort guarantees every pre-window txn precedes every in-window one, so a buy *on* the start date is correctly treated as an in-window flow, **not** opening capital (no double count). When the window has no in-window transactions at all, the lots are unchanged across it, so they're valued at the start date as the opening. The terminal value reuses the last sample point's market value (cost fallback already applied).

**Display.** A "**Money-weighted (IRR): ±X.X% / yr**" line under "Return on cost" in the summary panel, coloured by sign, with a one-line caption ("annualized — accounts for timing & size of buys, sells and distributions"). `—` when undefined; an approximate-flag note when any bookend/transfer fell back to cost; display capped at ±999.9%/yr (short windows annualize to extremes).

**Verified** headless on a WAL-consistent backup-API snapshot of the live DB (`compute_returns` + `xirr`, then an independent NPV-at-IRR ≈ 0 reconciliation): whole-portfolio **MAX (2010→2026) IRR = +10.0%/yr** on +$815.6k total return (a sane 16-year figure where the headline total-return mixes in lifetime unrealized); **YTD +29.7%/yr** and **1Y +29.5%/yr** — far more honest as *period* returns than the YTD total-return figure. Six synthetic edge-case checks all matched closed-form: a buy on the window start is a flow not opening capital (IRR = 2^(1/4)−1); a pre-window holding books its opening MV without double-counting; a no-in-window-txn window still values opening from the nearest-prior price; sell-vs-hold give identical IRR (realized/unrealized invariance); a cash dividend lifts IRR; a reinvested dividend adds no flow yet still counts as income. Offscreen Qt (PySide6 6.11.1) confirmed the window renders the IRR line + approximate-note.

**Trade-off / still open.** IRR uses **one combined cash-flow stream**, so it's a *portfolio* money-weighted return — there's no per-security IRR column yet (per-security would need per-security flow lists; a clean future extension). Annualization makes short windows (MTD/last-month) volatile — flagged by the caption, not suppressed. Unpriced opening/terminal/transfer legs fall back to cost and set `irr_fully_priced=False` (the owner's 3 uncovered orphan tickers trip this); the figure is then approximate, noted in the panel.
