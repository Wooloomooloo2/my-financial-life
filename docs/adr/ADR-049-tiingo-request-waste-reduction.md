# ADR-049 — Tiingo request-waste reduction: orphan-skip, give-up memory, and 429 back-off

**Date:** 2026-06-09 (amended 2026-06-10, 2026-07-12)
**Status:** Accepted (amended — see *Amendment 2026-06-10* and *Amendment 2026-07-12* at the foot)
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

**Refinement — fetch from the first-transaction floor, not 1900.** The owner asked: *"why isn't the start date the date of the earliest transaction? I don't need history going back to 1900 for a security I only bought in 2023."* Correct — a security is never held before its first trade, and no computation (holdings, value-over-time, returns) ever reads a price earlier than that, so fetching from 1900 only stores decades of dead rows. The two pricing queries (`securities_missing_history`, `securities_to_price`) now carry an `earliest_txn_date` per security — `date(MIN(txn.posted_date), '-7 days')`, a 7-day buffer so a *nearest-prior* lookup around the buy date still resolves — on the `SecurityRow` dataclass; the single-security path (`backfill_security_history_into`, the Stock Record button) uses the new `Repository.earliest_transaction_date(security_id)`. The three backfill functions pass that floor as `start_date`. **`HISTORY_START_DATE` (1900-01-01) survives only as the fallback** for the rare case of a security with no transactions (an orphan manually fetched). This is a *storage/cleanliness* win, not a request-count one — it's still one request per ticker — but it keeps `security_price` proportional to the holding period and the value-history chart's month-end sampling cheap. An explicit caller `start_date` still overrides the floor.

**Verification.** Headless, no quota spent. URL construction: `fetch_historical('DIVO')` with no start emits `…&startDate=1900-01-01` (fallback) and returns the full series; an explicit `start_date` passes through verbatim; `_fetch_one` emits no `startDate`. Floor wiring (on the live DB / a checkpointed copy): `securities_to_price` returns 32 held tickers each with a populated `earliest_txn_date` (e.g. DIVO 2022-06-10, TSLA via `earliest_transaction_date(68)` = 2021-03-15 = first trade 2021-03-22 minus 7d); `backfill_security_history_into(68,'TSLA')` emits `…&startDate=2021-03-15`. A live positive test (full series actually stored) was blocked by the active 429 window at diagnosis time — owner to confirm by clicking **Backfill history** once the window clears.

---

## Amendment 2026-07-12 — the latest-price sweep re-fetched closes it already had

**Symptom.** The owner: *"I just updated my prices and immediately hit my Tiingo limit. Did something change? Latest price updates should never hit a limit."* The live DB agreed — a successful sweep stamped `tiingo_last_refresh_at = 2026-07-12T09:00:39Z`, and a 429 back-off was written **ten seconds later** at `09:00:49Z`.

**Nothing had changed in the code** — `prices.py` was untouched since the amendments above. What changed was the **portfolio**. Tiingo's free tier allows **50 requests per hour** (1,000/day, 500 unique symbols/month), the daily endpoint is one request per ticker, and the owner now holds **32 unique tickers**. So one sweep costs 32 requests, and *two sweeps in the same hour cost 64* — over the cap. The launch worker (`__main__`) runs `refresh_latest_prices_into` automatically; the toolbar's **Update prices** then calls it again with `force=True`. Two sweeps, one hour, 429. At ~20 tickers the same two clicks cost 40 and nobody noticed.

**The actual defect, though, is that neither sweep could have returned anything.** 2026-07-12 was a **Sunday**. The last close was Friday 2026-07-10, and all 32 tickers already had it stored. The correct request count for that refresh was **zero**; we spent 64. `refresh_latest_prices_into` throttled on *when the sweep last ran* (24h, and `force` skipped even that) but never asked the only question that matters: **do we already hold the latest close for this security?** Cost therefore scaled with portfolio size on every single click, forever, including on days the market never opened.

This is the same insight the 2026-06-10 follow-up applied to the **Backfill history** button — *"a re-click only spends Tiingo requests on securities that need them; a complete, up-to-date series costs zero"* (`securities_with_incomplete_history`). That reasoning was simply never carried across to the latest-close path, which is the one that runs on every launch and every toolbar click.

**Decision — skip any security already holding the latest published close.**

1. **What "latest" means.** `expected_close_date(now)` returns the most recent date whose EOD close should exist: today only counts once past `CLOSE_PUBLISHED_UTC_HOUR` (22:00 UTC — US markets close 20:00/21:00 UTC and Tiingo settles after; erring late costs a few hours' staleness, never a wasted portfolio-wide sweep against a close that doesn't exist yet), and Saturday/Sunday walk back to Friday.

2. **The skip.** `Repository.latest_market_price_dates()` gives the newest price date per security; anything at or past the target is dropped before a request is made. Only **`manual` / `tiingo`** rows count — a `transaction`-sourced row is the owner's own trade print seeded by `seed_prices_from_transactions`, not that day's close, and if it counted then a security bought on Friday would look priced-through-Friday and never get a real price. `force=True` keeps its meaning of "ignore the 24h clock" and explicitly does **not** mean "re-download what I already have".

3. **Holidays are learned, not calendared.** Maintaining an exchange calendar (and its per-market divergence) is a standing liability for a handful of days a year. Instead: the first sweep on a shut-but-expected trading day comes back with an older close than asked for, and we remember that pairing (`tiingo_close_seen_for` = the date we expected, `tiingo_market_close_date` = what the market last actually closed). Every later refresh that day lowers its target accordingly, finds everything current, and spends nothing. Come the next day the expectation moves on, the pairing goes stale, and a real fetch happens.

4. **"0 prices" now reads as success.** `RefreshResult.skipped_count` is populated, the Securities dialog appends *"· N already up to date"*, and the toolbar says *"0 prices (already up to date)"* rather than a bare *"0 prices"* that looks like a silent failure.

Rejected: **a request-budget pacer** (the heavier follow-up deferred twice above). Still the right eventual answer for a first-run catch-up that genuinely needs >50 requests, but it rations waste rather than removing it — and once you don't re-fetch what you already hold, steady-state cost drops to *zero on most days and ~N once a day at most*, which is comfortably inside the cap and makes the pacer far less urgent. Also rejected: **raising the launch throttle / disabling the launch refresh**, which trades away freshness to work around a bug that had nothing to do with frequency.

**Consequences.** A refresh when you're current costs **0 requests** (weekend, holiday, or thirty seconds after the last one); a Monday-evening refresh costs one request per stale ticker. The launch sweep plus any number of toolbar clicks can no longer breach the hourly cap on their own. Headroom now scales with *how many securities are genuinely stale*, not with portfolio size.

**Verification.** New `tests/test_prices_refresh_request_cost.py` (7 tests) stubs the client and **counts the tickers actually requested**: already-current → `[]`; stale → all fetched; mixed → only the stale one; a second forced sweep in the same hour → `[]`; a `transaction`-seeded row does *not* suppress a fetch; the weekend/pre-close/post-close target dates; and the holiday-repeat case → `[]`. Against a checkpointed copy of the live file, dated to the reported Sunday: target `2026-07-10`, **0 tickers requested, 33 skipped as current** — down from 32 requests. Full suite green.

**Residual.** Two of the owner's securities carry **CUSIPs** as symbols (`46593LUT4`, `037833ET3` — Apple 4% 05/10/2028). Tiingo is ticker-only, so these can never resolve: they burn one request each until the 30-day give-up cooldown catches them, then burn them again a month later, in perpetuity. Bond pricing needs a different provider (or none) — out of scope here, but they should not be sitting in the pricing queue at all.
