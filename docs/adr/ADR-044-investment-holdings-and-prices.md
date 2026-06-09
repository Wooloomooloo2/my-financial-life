# ADR-044 — Investment holdings (FIFO) + prices (Tiingo + manual) + market value

**Date:** 2026-06-09
**Status:** Accepted (round 2 shipped)
**Related:** ADR-043 (round 1 — `security` master + `txn` investment columns; this round reads that data), ADR-010 (the dormant `lot`/`valuation` tables — `lot`'s accounting model is realised here, computed not persisted), ADR-019 (Net Worth deferred investment market value to "when the valuations UX ships" — this closes that follow-through), ADR-035 (the openexchangerates client + settings-API-key + background-launch-refresh pattern that the Tiingo integration mirrors).

---

## Context

Round 1 (ADR-043) imports investment QIF into an interleaved register and keeps cash balance = `SUM(amount)` correct, but an investment account had **no holdings view** (shares per security, cost basis, gain) and **no prices**, so its *market value* was unknown — Net Worth, the sidebar, and the per-account summary showed only its cash balance, under an amber "valuations not yet wired" banner.

The owner asked for **a holdings screen and a way to get prices (current; historical later)**. Decisions (confirmed):

- **Prices: Tiingo** (free API key) for the ~35 tickered securities — best mutual-fund + ETF coverage for this fund-heavy portfolio — **plus always-available manual entry**, which is mandatory because **56 of the 91 securities carry no ticker** and no API can price them.
- **Cost basis: FIFO lots** (the owner's account is a USD E*Trade account; US-style per-lot accounting). This realises the `lot` accounting model that's sat dormant since ADR-010.
- **History: current prices only** this round; historical backfill + value-over-time is round 3.

---

## Decision

### Data model — migration `0013_security_prices.sql`
A date-stamped per-security price store, shaped like `fx_rate` (composite PK `(security_id, price_date)`, a `(security_id, price_date DESC)` index for latest/nearest lookups, a `source` of `manual`|`tiingo`). **Not** the account-level `valuation` table: prices are a per-security time series and an investment account's value is *derived* (cash + Σ shares × price), not a stored account valuation. `price` is REAL (a per-share quote, like `txn.price`/`lot.unit_cost`); money stays integer pence. New settings: `tiingo_api_key`, `tiingo_last_refresh_at`.

### FIFO holdings engine — `mfl_desktop/holdings.py` (pure Python)
Mirrors `account_summary.py`: takes the account's `TransactionRow`s + opening balance + a latest-price map, returns `Holding` rows + a `HoldingsView`. Replays transactions in `(date, id)` order maintaining a per-security FIFO deque of lots — share-ins push a lot, share-outs consume oldest-first accruing realized gain = proceeds − matched cost. Share direction comes from the **shared** action sets now in `mfl_desktop/import_engine/qif_actions.py` (extracted from `qif_parser.py` so the import sign-mapping and the holdings share-mapping can't drift).

**Computed on the fly, NOT persisted to `lot`.** FIFO is recomputed from the transactions on every call, so there is no second source of truth to keep in sync with edits/imports. The `lot` table stays reserved for the case that genuinely needs *persisted* lots — manual cost-basis overrides on transferred-in shares, or specific-ID (vs FIFO) sale selection — which isn't in scope here.

**Lot cost** = the true net cash for cash-funded buys (`abs(txn.amount)`, which includes commission per ADR-043); for reinvestments/`ShrsIn` it's `price × qty`. **Edge cases, all flagged `basis_incomplete`:** `ShrsIn` shares with no price (basis unknown → lot cost 0), oversells beyond known lots (the engine clamps at zero shares rather than going negative), and stock splits (ratio application deferred — skipped with a log note; the owner's only split is a malformed empty row). **Whole-account transfers (`XIn`/`XOut`) do not move lots** here — that's the round-4 transfer-linking concern, and it sidesteps a QIF quirk in the owner's data (a transfer-out mislabelled `XIn`).

### Prices client — `mfl_desktop/prices.py` (urllib, Qt-free; mirrors `fx.py`)
`TiingoClient.fetch_latest(symbols)` hits `/tiingo/daily/<ticker>/prices` once per ticker (so one unsupported fund ticker fails only itself), returning the latest close + date. `refresh_latest_prices_into(repo, force=False)` skips when there's no key / no tickered securities / a refresh < 24h ago, upserts `source='tiingo'` prices, and records the timestamp only on success (retry-next-launch). A `_PriceRefreshRunnable` in `__main__.py` runs it on launch in a background thread with its own Repository connection, silent on failure.

### Repository
`upsert_security_price`, `latest_prices() -> {security_id: PriceRow}`, `latest_price_for_security`, `list_securities_with_symbol`, `PriceRow`. **`compute_account_values()`** — the market-value sibling of `compute_account_balances()`: for `family=='investment'` it returns `cash + Σ(open-lot shares × latest price)` via the holdings engine (unpriced holdings contribute nothing, so a no-prices account falls back to cash); every other family is the cash formula unchanged. (Lazy-imports `holdings` to avoid the import cycle, since `holdings` imports `TransactionRow`.)

### UI
- **Manage → Securities…** (`securities_dialog.py`, mirrors the Currencies dialog): Tiingo key field + disclaimer + last-refresh label, synchronous **Refresh Now** (wait cursor), a table of every security with its latest price (or "—", so the user sees what still needs pricing), and a **manual price** row — the universal fallback.
- **Holdings panel on the per-account summary:** for investment accounts the bottom Top-Payees/Top-Categories row (meaningless for a brokerage) is replaced by a full-width Holdings table (Symbol / Security / Shares / Avg cost / Cost basis / Last price / Market value / Unrealised gain, gain coloured, `basis_incomplete` marked `*` + tooltip) with an Account-value / Unrealised / Realised / Cash totals line. Formatted in the account's own currency (the screen's other figures are GBP-hardcoded — a separate display-currency backlog item). The amber banner softens to an "N holdings unpriced" nudge, shown only when something lacks a price.
- **Net Worth + sidebar balances** read `compute_account_values()` so an investment account contributes market value (cash fallback when unpriced). The **register's running-balance column is untouched** — it's a cash ledger.

---

## Consequences

### Positive
- The owner gets a real holdings view and market-value net worth. Verified on the live E*Trade data: **30 open positions, $162k cost basis, FIFO realized gain $2,672** lifetime (the Tesla round-trip — buys 10@684.98 / 1@642.94 / 7@642.957, sell 18@685.985 — checks out at **+$354.00**, fully closed). Injecting one manual price flows straight through: shares × price = market value, account value = cash + Σ market value, Net Worth updates.
- The Tiingo integration reads identically to the FX one (same client/refresh/dialog/launch-runnable shape), so there's one pattern to maintain.
- The cash ledger is untouched; market value is a derived, additive layer.

### Negative / trade-offs
- **FIFO is recomputed, not stored.** Cheap at personal-finance scale and always-correct, but means no persisted per-lot record to hand-edit yet — deferred until a manual-basis/specific-ID need is real.
- **56 securities can't be auto-priced** (no ticker) and need manual prices; until priced, they contribute nothing to market value (the account falls back toward cash, and the summary nudges).
- **Stock-split ratios aren't applied** and transferred-in shares may have unknown basis — both flagged `basis_incomplete` rather than silently wrong; full handling waits for manual investment editing / round 4.
- **Tiingo needs a per-user free key** — fine for the owner; when the app is shared, each user adds their own (manual entry works without one).

### Ongoing responsibilities
- **`amount` stays the signed cash impact** for investment rows (holdings derive shares from `quantity`+`action`, never re-sign amount).
- **The action sets live once** in `qif_actions.py` — extend there, and both the importer and the holdings engine stay in agreement.
- Historical prices (round 3) layer onto the same `security_price` table (the date stamp is already there) and the same Tiingo client (`fetch_historical`).
