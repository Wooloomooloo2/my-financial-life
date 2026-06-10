# ADR-049 — Tiingo request-waste reduction: orphan-skip, give-up memory, and 429 back-off

**Date:** 2026-06-09 (amended 2026-06-10)
**Status:** Accepted (amended — see *Amendment 2026-06-10* at the foot)
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

---

## Amendment 2026-06-10 — the history backfill was never actually fetching history

**Symptom.** The owner ran "pull all history" again and reported it *"didn't really seem to update much history and told me I used all my tokens again in one update."* Inspection of the live DB: **42 `security_price` rows of `source='tiingo'` across 36 tickered securities, every row dated 2026-06-08 or -09** — i.e. nothing but the last two days' latest-close refreshes. No security had an actual historical series, despite the backfill having been run repeatedly.

**Root cause — a defect in the original ADR-044 client that this very ADR's narrative mis-stated.** The Context section above asserts that Tiingo's daily endpoint *"returns a security's entire series in one HTTP request regardless of date range."* That is only true **when a `startDate` is supplied.** `GET /tiingo/daily/<ticker>/prices` **with no date parameters returns only the latest single end-of-day row.** `TiingoClient.fetch_historical` called `_request(sym, start_date=None)` and its docstring claimed *"None lets Tiingo return its default window (several years)"* — both wrong. So every history backfill (`backfill_historical_into`, `backfill_missing_history_into`, `backfill_security_history_into`) fetched exactly **one row** per ticker, identical to a latest-refresh.

**This silently defeated waste-stopper §2's premise.** A security only leaves `securities_missing_history(min_points=2)` once it has ≥2 real prices. Because the backfill never landed more than one row, securities never crossed that threshold, so `backfill_missing_history_into` re-fetched ~all of them on **every launch** — and `refresh_latest_prices_into` fetched ~all of them again — ≈60 requests/launch, repeatedly, blowing the 50/hour cap. The orphan-skip and 429 back-off shipped in this ADR were real and working; they were masking, not fixing, this deeper bug (the back-off was firing because the backfill kept generating ~60 requests it shouldn't have needed to).

**Fix (one change, centralised).** `fetch_historical` now sends `HISTORY_START_DATE = "1900-01-01"` (a new module constant) when the caller passes no `start_date`. Tiingo clamps it to each security's inception, so one request returns the whole series — which is what the rest of this ADR always assumed. `_fetch_one` / `refresh_latest_prices_into` are unchanged: they deliberately send **no** `startDate` because they genuinely want only the latest row. An explicit `start_date` from a caller is still respected.

**Knock-on benefit — this also fixes the request-volume complaint at steady state.** Once a backfill lands a full multi-year series, those securities cross `min_points=2` **permanently** and drop out of `securities_missing_history` for good, so the per-launch re-fetch storm stops; steady state collapses to the 24h-throttled latest-refresh (~35 tickers < 50/hour).

**Known residual (accepted, not fixed here).** The *first* catch-up launch after this fix still wants ~23 history + ~35 latest ≈ 58 requests, over the 50/hour cap, so it 429s partway. But now **partial progress sticks** (each backfilled security drops out permanently) and the §3 back-off window prevents wasteful retries, so it self-heals across ~2 hourly windows. A proactive request-budget pacer remains the deferred heavier follow-up (already noted under Consequences). Also note: ~9 securities that accumulated 2 *latest-close* rows during the bug era now satisfy `min_points=2` and so won't auto-backfill — the deliberate **Backfill history** button (`backfill_historical_into`, which fetches all held tickers regardless of count) is the way to pull their real history.

**Verification.** Headless URL-construction test (no quota spent): `fetch_historical('DIVO')` now emits `…/prices?…&startDate=1900-01-01` and returns the full series; an explicit `start_date` is passed through verbatim; `_fetch_one` still emits a URL with no `startDate`. A live positive test (full series actually returned) was blocked by the active 429 window at diagnosis time and is to be confirmed by the owner clicking **Backfill history** once the window clears.
