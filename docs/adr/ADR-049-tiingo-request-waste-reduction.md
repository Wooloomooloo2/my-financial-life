# ADR-049 — Tiingo request-waste reduction: orphan-skip, give-up memory, and 429 back-off

**Date:** 2026-06-09
**Status:** Accepted
**Related:** ADR-044 (Tiingo price client + `security_price` + the launch refresh — this constrains all of it), ADR-047 (auto-backfill via `securities_missing_history` + `_PriceRefreshRunnable` — the very paths that were re-fetching every launch; also the source of the "uncovered ticker retried per launch" open item this closes), ADR-043 (`security` master + the `txn.security_id` link used to tell a held security from an orphan).

---

## Context

The owner blew through Tiingo's free-tier limits (**1000 requests/day, 50/hour**) "very quickly" while building out historical pricing, and proposed dropping daily history to **weekly/monthly** to use less quota.

**The proposal rests on a misconception worth recording.** Tiingo's daily endpoint (`GET /tiingo/daily/<ticker>/prices`) returns a security's *entire* series in **one HTTP request** regardless of date range; `resampleFreq` is just a query param on that same single call (`prices.py::_request` does one `urlopen` per symbol). The rate limit counts HTTP requests, not rows. So daily vs weekly vs monthly is **one request either way** — coarser granularity reduces *stored rows*, not *API calls*, and would not have fixed the limit. The owner, once shown this, chose **keep daily** and **"stop the waste first."**

Where the requests actually go (per launch, ~35 tickered securities in the owner's data):

1. **Orphan securities are fetched.** The owner imported the E\*Trade QIF, whose `!Type:Security` block lists securities held in **other Banktivity accounts not yet migrated**. Those securities exist in the master with **zero transactions** in MFL — yet `list_securities_with_symbol()` returns every tickered security, so the launch backfill and latest-refresh fetch prices for holdings that contribute to no view at all. Pure waste, and it grows as more un-migrated tickers ride in on future imports.
2. **Uncovered tickers are retried every launch.** A tickered security Tiingo can't serve (a UK fund, an old managed-account holding) never reaches ≥2 stored prices, so `securities_missing_history` keeps handing it back and `backfill_missing_history_into` re-fetches it on **every** launch — exactly the open item ADR-047 flagged.
3. **No back-off when limited.** On HTTP 429 the client just raised a generic `PriceFetchError` collected as a string; the loop kept calling for every remaining ticker, each one a guaranteed 429 — burning the rest of the hour's allowance confirming we're rate-limited.

Scope confirmed with the owner: **stop the waste, don't build a budget meter (yet).** A full per-hour/per-day request-budget tracker with resumable backfill is the heavier follow-up; this ADR is the cheap, high-leverage subset.

---

## Decision

Three independent waste-stoppers, all in the price layer (`prices.py` + `Repository`), no UI change.

### 1. Skip orphan securities — price only what's held
A security with no transactions has no holding, no market value, no return, and no place in any chart — fetching its price is wasted. New `Repository.securities_to_price(*, cooldown_days, as_of)` returns tickered, non-archived securities that have **at least one transaction** (`EXISTS (SELECT 1 FROM txn …)`) and are not currently given-up (see §2). `securities_missing_history` gains the same `EXISTS` + give-up filter. `refresh_latest_prices_into` and `backfill_historical_into` switch from `list_securities_with_symbol()` to `securities_to_price()`. Orphans stay in the master untouched and fully visible in Manage ▸ Securities — they simply aren't auto-priced **until a transaction references them** (i.e. once the owner migrates the account that holds them). `list_securities_with_symbol()` keeps its original "every tickered security" meaning for any non-pricing caller.

### 2. Give-up memory for uncovered tickers — migration `0016`
`migration 0016_security_price_fetch_status.sql` adds a nullable `security.price_fetch_failed_at TEXT` (ISO datetime). When a **history** fetch for a security comes back as "Tiingo doesn't cover this ticker" — an HTTP 404 (`SymbolNotFoundError`) or a successful-but-empty series — the loop calls `Repository.mark_security_price_unavailable(id)`; a fetch that stores any rows calls `clear_security_price_unavailable(id)`. The give-up is a **cooldown, not a tombstone**: `securities_missing_history` / `securities_to_price` exclude a security only while `price_fetch_failed_at` is within `cooldown_days` (default **30**), so a ticker that Tiingo starts covering, or that the owner later corrects, is retried automatically after a month. The **single-security explicit path** (`backfill_security_history_into`, the Stock Record "Fetch from Tiingo" button) ignores the cooldown and clears/sets the flag on its result — it's the user's manual override.

Why a column on `security` rather than a JSON blob in `setting`: the give-up state is per-security domain data, and putting it on `security` lets the exclusion live in the same SQL `WHERE` as the orphan filter (one query, no Python post-filter). It mirrors how `archived_at` already lives on the row.

### 3. 429 back-off — one persisted timestamp
The client gains two `PriceFetchError` subclasses: `RateLimitedError` (HTTP 429, carrying `retry_after_seconds` parsed from a `Retry-After` header when present) and `SymbolNotFoundError` (HTTP 404). `_request` raises the right one by status code. On `RateLimitedError`, each fetch entry point records `setting['tiingo_rate_limited_until']` = now + `Retry-After` (or a **1-hour** default — long enough to clear the 50/hour cap, short enough to recover same-day) and **stops the loop** (every further call this run is a guaranteed 429). Every entry point first calls `_is_rate_limited(repo)` and no-ops with a clear error string while the window is open, so a relaunch during the back-off window costs **zero** requests. The window self-expires; no clearing logic needed.

These compose: launch order stays `seed_prices_from_transactions` → `backfill_missing_history_into` → `refresh_latest_prices_into`. The first to hit a 429 sets the window; the rest short-circuit immediately. Orphans and given-up tickers never enter any loop.

---

## Consequences

- **Steady-state launch cost drops to "tickered securities you actually hold, with working tickers, that don't already have history."** Once backfilled, that's ~0 history calls + one latest-refresh sweep per 24h — and that sweep now also skips orphans and given-up tickers.
- **A 429 costs ~1 wasted call**, not the rest of the hour's allowance.
- **Newly migrated accounts self-heal:** the moment a transaction references a previously-orphan security, it enters `securities_to_price` and gets backfilled on the next launch — no manual step.
- **Granularity stays daily** — the Stock Record mini-chart keeps full resolution; the value-history chart's month-end sampling was always a read-time concern, never a fetch-time one.
- **Deferred (explicitly):** a real **request-budget tracker** (rolling hour/day counters, resumable partial backfill that picks up where it left off) is the next step if the owner imports many more tickered accounts; this ADR's back-off is reactive (stop once told no), not proactive (pace to stay under). Also deferred: surfacing give-up state in the Securities/Stock Record UI (a "Tiingo: not covered" hint) and a manual "retry now" that clears the cooldown — the per-security Fetch button already bypasses it.
- **Rejected — reduce history granularity to weekly/monthly:** doesn't reduce request count (one request per ticker regardless), so it can't address a rate-limit problem; it only trims stored rows at the cost of chart detail. Kept daily.
- **Rejected — delete/auto-archive orphan securities:** destructive and premature; the owner intends to migrate the accounts that hold them. Skipping them at fetch time is reversible and needs no decision about data the owner still wants.
