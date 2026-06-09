# ADR-047 — Investment pricing: automation, transaction-derived prices, and the Stock Record screen

**Date:** 2026-06-09
**Status:** Accepted
**Related:** ADR-044 (Tiingo price client + `security_price` + manual entry + market-value net worth — this extends all of it), ADR-045 (historical-price backfill + value-over-time — closes its "auto-backfill newly-seen securities" follow-up), ADR-043 (`security` master + `txn` investment columns — the per-share `txn.price` is the seed source), ADR-046 (`compute_returns` — benefits directly once Tesla gets a ticker), ADR-026 (hand-rolled paintEvent charts + `chart_helpers`; [[feedback-chart-engine-preference]]), ADR-032/0008 + ADR-046/0014 (CHECK-widening table-rebuild pattern reused by migration 0015).

---

## Context

After ADR-044/045 the price pipeline worked but had four gaps that blocked real use of the owner's data (91 securities: **35 tickered, 56 not**):

1. **The untickered majority couldn't be priced automatically.** 56 holdings (the managed-account era) carry no ticker, so Tiingo can't fetch them and they fall back to cost basis on every chart. The owner's insight: *a Buy/Sell of an unlisted fund is itself a price observation* — Fund XXYYZZ bought 15 Jan 2023 at $15.25 means $15.25 on that date. Their own trades semi-automate their price history.
2. **No way to set a missing ticker.** Tesla Inc (id 68) imported with `symbol = NULL` even though it's TSLA, so it never got Tiingo prices. The register's investment columns are read-only and the Securities dialog couldn't edit the master. Confirmed in the live DB: a **single** Tesla record (no duplicates) — editing the symbol on the master is the whole fix; no merge-securities feature needed.
3. **Historic prices needed a manual button click.** `backfill_historical_into` only ran from the Securities dialog. Newly imported securities never got history until someone remembered to click "Backfill history".
4. **No per-security detail view.** `security_price` was only surfaced as one "latest price" per row; nowhere to view/audit/edit a security's full price history or activity.

**Decisions confirmed with the owner:**
- **Transaction-derived prices apply to untickered securities only** — a tickered security keeps its clean Tiingo end-of-day series (an intraday execution price ≠ the EOD close).
- **Price-source precedence: `manual` > `tiingo` > `transaction`.** A user-typed price is never auto-overwritten; a Tiingo fetch overwrites tiingo/transaction but not manual; a transaction-derived price only overwrites a prior transaction-derived one.
- **Symbols are edited on the security master**, not per-transaction — fixing TSLA once flows to all four Tesla rows.
- **The Stock Record screen v1** shows editable identity (+ Tiingo re-fetch), a price-over-time mini chart, a price-history table (add/edit/delete), the security's transactions across all accounts, and the current position.

---

## Decision

### Storage — migration `0015_security_price_transaction_source.sql`
Widen the `security_price.source` CHECK from `('manual','tiingo')` to `('manual','tiingo','transaction')`. SQLite can't ALTER a CHECK, so the table is rebuilt (copy → drop → rename → recreate `idx_security_price_latest`), the same approach as ADR-032/0008 and ADR-046/0014. No data backfill in the migration — seeding runs in Python so it re-runs on every import and at launch.

### Price-source precedence — centralized in the Repository upserts
`Repository._PRICE_OVERWRITE_GUARD` maps a source to the `WHERE` appended to an upsert's `ON CONFLICT DO UPDATE`: `manual` → unconditional; `tiingo` → `WHERE security_price.source != 'manual'`; `transaction` → `WHERE security_price.source NOT IN ('manual','tiingo')`. `upsert_security_price` picks its guard from `source` (callers unchanged); `bulk_upsert_security_prices` (the Tiingo backfill path) gains the `!= 'manual'` guard. So a backfill or a launch refresh can never clobber a hand-typed price.

### Transaction-derived prices — `Repository.seed_prices_from_transactions(security_ids=None)`
One set-based statement: for every `txn` with `price IS NOT NULL AND price > 0` whose `security.symbol` is blank, insert `(security_id, posted_date, price, source='transaction')` with the transaction-precedence conflict guard. `price IS NOT NULL` naturally selects trades/reinvests and skips cash `Div`/`Cash` rows (which carry no price) — no action-set filtering needed. Idempotent. Called (a) after each investment import commit (scoped to the just-imported securities; wrapped so a hiccup never fails an otherwise-successful import), and (b) once at launch (instant, no network, no key) so the existing history gets seeded.

### Automated download — current + historic-if-missing
- `Repository.securities_missing_history(min_points=2)` — tickered, non-archived securities with `< min_points` *real* (`manual`/`tiingo`) stored price rows. **Transaction-derived prices don't count as history**, so giving a previously untickered holding a ticker doesn't let its handful of trade-seeded points mask it — the real end-of-day series is still fetched.
- `prices.backfill_missing_history_into(repo)` — full Tiingo history for **only** those securities (one call each). Self-limiting: once backfilled a security drops off the list, so a daily launch doesn't re-fetch everything — only newly tickered/imported securities.
- `__main__._PriceRefreshRunnable` now runs three steps cheapest-first: `seed_prices_from_transactions()` → `backfill_missing_history_into()` → `refresh_latest_prices_into()` (the existing 24h-throttled latest close). Still silent-on-failure on its own connection.

### Editable master + per-security reads — `repository.py`
- `update_security(id, *, name=None, symbol=None, type_=None)` — `None` = leave unchanged; `""` clears symbol/type to NULL. Rejects a blank or duplicate name. Setting a symbol re-enables Tiingo for that holding. (Uses `None`-as-unchanged rather than the class `_UNSET` sentinel — none of these fields legitimately take `None` as a stored value, and it sidesteps pitfall #7 for a method defined before the sentinel.)
- `list_transactions_for_security(id)` — every investment row referencing the security across all accounts (mirrors `list_all_transactions`' join).
- `delete_security_price(id, price_date)`.

### Stock Record screen
- **`ui/price_history_chart.py`** — a compact single-line paintEvent chart over `[(date, price)]`, modeled on `value_history_chart.py` (reuses `nice_ticks`, axis/hover scaffolding). Currency-neutral (`$` prefix; prices are quotes, not pence). Single-point and empty states handled.
- **`ui/stock_record_dialog.py`** — `StockRecordDialog(QDialog)`: an editable identity header (name / ticker / type) with **Save details** (`update_security`) and **Fetch from Tiingo** (saves the ticker, then `prices.backfill_security_history_into` for that one security; enabled only when a ticker is present); a **Current position** card computed by reusing `holdings.compute_holdings_view` over just this security's transactions (so FIFO basis / realized gain match the holdings table and returns report — a closed position shows lifetime realized gain); the **price mini chart**; a **Stored prices** table (add/edit via `upsert_security_price(source='manual')`, delete selected); and a **Transactions** table.
- **Entry point** — `ui/securities_dialog.py`: the prices table becomes row-selectable; double-click a row (or the new **Open stock record** button) opens `StockRecordDialog` modally; on return the latest-price table reloads.

---

## Consequences

### Positive
- **Verified on a copy of the live DB:** migration 0015 applies cleanly; seeding gives Tesla 3 price points from its 4 trades (the two same-day buys correctly collapse to the last); tickered securities get **zero** transaction-source rows; precedence holds (a manual price survives a later transaction seed *and* a Tiingo upsert; Tiingo overwrites a transaction row). After `update_security(68, symbol="TSLA")` the Stock Record shows the seeded prices, the 4 transactions, and a fully-exited position with **realized gain $354.00** (matching the ADR-046 Tesla round-trip). All dialogs construct headless (offscreen) with no import/NameError gaps.
- The untickered majority now builds price history from its own trades with no user effort; the explicit Stock Record manual table + Tiingo-after-ticker remain for the rest.
- Closes the ADR-045 "auto-backfill newly-seen securities" follow-up; Tesla now appears in the investment reports once tickered.

### Negative / trade-offs
- **Transaction-derived prices are execution prices on trade dates only** — sparse and intraday-ish, not a daily NAV series. That's why they're untickered-only and the lowest-precedence source; setting a ticker + Fetch replaces them with real EOD data on the dates Tiingo covers.
- **A Tiingo-uncovered ticker** (a fund Tiingo lacks) with no real prices stays in `securities_missing_history` and is retried (one failed call) each launch — cheap and silent, but not zero. (`securities_missing_history` counts only `manual`/`tiingo` rows, so transaction-seeded prices don't mask a newly tickered security — setting a ticker *does* pull the full history on the next launch, and the Stock Record **Fetch from Tiingo** button is the immediate path.)

### Ongoing responsibilities
- Any new code path that writes a price must go through the guarded upserts (or set `source` correctly) or it can silently clobber a manual entry — the precedence lives in the Repository, not the schema.
- A future **merge-securities** verb is still unbuilt; with 56 untickered holdings there may be genuine duplicates elsewhere (Tesla was not one). Out of scope here.
- Per-account currency on the Stock Record screen is hardcoded `$` (the portfolio is USD); a currency-aware version is a later refinement.
