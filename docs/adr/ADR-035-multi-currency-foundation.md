# ADR-035 — Multi-currency foundation: per-account currency, FX rate table, transfer parent row, and the openexchangerates.org integration

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-010 (Transactional schema — `account.currency` already on every account, `person.base_currency` already on the profile); ADR-020 (Transfers — two linked txns sharing one `transfer_id`); ADR-018 (Spending Over Time — needs a display-currency selector); ADR-019 (Net Worth — same); ADR-024 (Budget — perimeter math needs to convert); ADR-033 (Per-account summary — stays native; only cross-account aggregations convert)

---

## Context

The owner is moving from a UK-only setup (everything in GBP) to a mixed estate with several USD accounts. The plan is to import the USD accounts at one go and have the app behave correctly across both currencies, including:

- Each account is in its native currency. A USD account stores its txn amounts in USD; a GBP account stores them in GBP. No silent conversion at storage time.
- Reports can be rendered in any currency the user picks. Cross-account aggregations (Net Worth, Spending Over Time, Budget tiles, Top-N panels in the per-account summary's cross-account drill-down) convert at display time.
- Transfers between accounts in different currencies store an explicit exchange rate so the two halves remain accurate over time even if the user later edits the rate or one side's amount.
- The rate source is automatic where possible — openexchangerates.org is the chosen provider — with a manual fallback for missing dates and historical loads.
- The owner has explicitly asked for a *visible refresh* affordance plus an automatic refresh, so the system is never silently stale.

Very few personal-finance apps do this cleanly. The reason is that retrofitting currency into a single-currency model is brutal — every aggregation has to gain a conversion call, every transfer flow has to learn a rate, every report has to gain a currency selector. The right call is to bake it in *now*, while the data is small and there are no USD txns yet, so the foundation is laid before the first non-GBP import.

The good news: the schema already has the two hooks needed for the storage side — `account.currency` (ADR-010 §account) and `person.base_currency` (ADR-010 §person). That means the foundation work is *additive* — no breaking change to `txn` or `account`, and existing rows interpret correctly under the new rules (everything is GBP today, the new conversion paths trivially return the input amount when both sides match).

This ADR is the *foundation* of the multi-currency arc. It locks: storage of FX rates, the transfer-rate model, the settings table, and the integration with openexchangerates.org. The UI surfaces (Currencies dialog, report currency selector, cross-currency transfer dialog) are described here at the level of *what each surface does*; the per-screen wiring details land alongside the implementation in the same turn.

Transfer matching to existing transactions is **out of scope** for this ADR — covered separately in [ADR-036](ADR-036-transfer-matching.md). Bulk reconcile is in [ADR-037](ADR-037-bulk-transfer-reconcile.md). The three ADRs were drafted as a set; ADR-035 is the schema/data layer the other two build on.

---

## Options considered

### Where does currency live on a transaction?

- *Per-txn currency column* on `txn`: each txn carries its own currency. Honest in principle but redundant — a txn belongs to exactly one account, and that account has exactly one currency. Adding the column duplicates information already on the FK target, which means it can drift (a USD txn ends up on a GBP account row with `txn.currency='USD'`, and now reports have to decide which side is right). Rejected.
- **Currency derived through `account.currency`** (chosen): a txn's currency is "the currency of its account, looked up via the existing FK." Every existing query is unchanged. The Repository surfaces a small `account_currency(account_id)` cache so cross-account aggregations don't pay a join per row.

### How are FX rates stored?

- *Bilateral, free shape* — `(date, base, quote, rate)` with no provider-imposed base: simplest API but every rate has to be entered once per direction.
- *USD-base only, derive cross-rates* — store rates as quoted against USD (because that's what openexchangerates.org's free tier provides), compute any GBP→EUR as `(USD→EUR) / (USD→GBP)`. Smaller storage; matches the provider; one place for the math.
- **Hybrid** (chosen): the table schema is the general `(date, base, quote, rate, source)` shape, so manual entries can fill in any bilateral pair the user types. But the *provider-fed* rows always have `base='USD'`. The lookup helper tries the requested pair directly first; if not present, falls back to the USD-pivot computation. Gives the flexibility of bilateral storage with the simplicity of USD-base ingest.

### What granularity?

- *Intraday rates*: too fine. Personal finance doesn't need it; openexchangerates.org's free tier doesn't expose it; storage explodes.
- **Daily snap** (chosen). One rate per `(date, base, quote)` pair, where `date` is the close of that day. txn lookups use the txn's posted_date.

### Missing-rate policy

- *Refuse to convert* — surface a banner, force the user to enter the missing rate before the report renders. Strictly honest but breaks the report on the first import that touches a date with no rate (e.g. weekends, before the user signed up for openexchangerates).
- **Nearest-prior fallback with a visible "approx" indicator** (chosen). If there's no rate for `date`, walk backward to the most recent rate that exists for that pair and use it. The report still renders; an "approx" badge in the corner makes the substitution visible. Weekends and bank holidays trivially fall through. The badge clicks through to the Currencies dialog if the user wants to fill the gaps.
- *Nearest-bidirectional* (use a future rate if no prior exists) — rejected. Personal finance always converts historical txns *against rates known at the time*; using a future rate is misleading.

### Where does the API key live?

- *Environment variable*: doesn't survive an `.exe` rebuild and is invisible to the non-technical user.
- *Per-file in the SQLite DB*: tied to the file, so a `Save Copy As…` snapshot carries the key — bad if the user shares the snapshot.
- **`setting` key-value table, scoped to the file** (chosen but with care): the key lives in the file the user is working with. The Currencies dialog explicitly warns "this key is stored inside the file; remove it before sharing a `.mfl` snapshot." Multi-file portability is acceptable in v1; refining storage to the OS keychain is a later ADR if real sharing happens.

This same `setting` table absorbs other app-level prefs that don't deserve their own column elsewhere — `transfer_match_window_days` (ADR-036), `oxr_last_refresh_at`, future UI prefs.

### How is the transfer's rate represented?

- *Inline columns on `txn`*: a `txn.rate` column on the destination row referring to the source. Cluttered (only meaningful for transfer rows), and the rate is one-per-pair, not one-per-row.
- *Derive on the fly from the two txn amounts*: `rate = |dest.amount| / |source.amount|`. Works perfectly for any same-data case. Falls apart if one side is later edited — the derived rate silently changes, and the historical "what rate was used" intent is lost.
- **A `transfer` parent row** (chosen): new table `transfer(iri PRIMARY KEY, from_account_id, to_account_id, rate, rate_source)` where the `iri` matches the existing `txn.transfer_id`. Stores the rate that was *used at posting time* plus its provenance (`derived` for same-currency transfers and back-filled rows; `manual` when the user typed it; `fx_rate` when looked up from the FX table). The two txn amounts on either side remain the truth-of-money; the `transfer.rate` row is the truth-of-intent.

Two important consequences:

1. `txn.transfer_id` stays TEXT and stays the link in `txn`, which means the existing register / delete / partner-aware queries don't change. The new `transfer.iri` matches by string.
2. Same-currency transfers populate a `transfer` row with `rate=1.0`, `rate_source='derived'`. Cheap, keeps the data model symmetric — every transfer has a parent row.

### How does the provider integration work?

openexchangerates.org's free tier:

- 1000 requests / month
- USD-base on the free plan (other bases need the paid Developer plan)
- `GET /latest.json?app_id=KEY` returns today's USD→* rates
- `GET /historical/YYYY-MM-DD.json?app_id=KEY` returns a single past day's USD→* rates

Strategy:

- **On launch**, if an API key is present and the last refresh was more than 24h ago, fetch `latest.json` in a background thread and upsert one row per quote currency the user has at least one account in.
- **Manual "Refresh Now"** in the Currencies dialog runs the same fetch synchronously with a small progress dialog.
- **Historical fill** is on demand: when a transfer or report needs a rate for date D that we don't have, we don't auto-fetch (would torch the monthly budget on a backfill). Instead the missing-rate path uses nearest-prior and surfaces the gap; the user can hit "Backfill historical" in the Currencies dialog for an explicit date range, which calls the historical endpoint once per missing day in that range (with a confirmation that names the cost in API calls).

Both auto and manual refresh paths write `setting.oxr_last_refresh_at` so the next launch can decide whether to fetch. Network errors degrade silently — the rates we already have remain usable; the missing-rate badge stays visible.

### Per-txn vs per-report currency control

- *Per-txn*: a manual transaction in a foreign currency on a domestic account. Edge case that complicates the schema (would need either the per-txn column we rejected above, or a separate "foreign amount" pair); rejected for v1.
- **Per-report** (chosen): every cross-account report has a display-currency combo. Defaults to `person.base_currency`. The selector lists every distinct currency present in the data plus the user's base (so picking "USD" on a GBP-only file still works as a what-if). Per-account screens (per-account summary, single-account register) render natively in the account's currency — no conversion at all.

### What about the running balance + cleared balance in the register?

The register and per-account summary show running balance / cleared balance / reconciled balance. These are *natively in the account's currency* — they're per-account. No conversion needed. The "All transactions" cross-account view already hides the Balance column (per `project-all-transactions-view`) because it's incoherent across currencies anyway — that decision is now ADR-backed instead of just being a UI hack.

### What about the budget perimeter?

A budget perimeter that includes accounts in different currencies is a real (if niche) case: a user with a UK current account and a US current account both contributing to "household groceries." ADR-024's perimeter math operates in pence (signed integers). For v1, **the budget's category amounts and tile totals are denominated in the file's base currency**, and the perimeter computation converts each txn at its posted_date before bucketing. A budget-level currency override is deferred — base-currency-only is the right v1 default since most users will set up their budget in their home currency.

### What about cash badges in headers?

The cash-on-hand badge in the budget header (ADR-024) and the recorded-balance line in the per-account summary (ADR-033) both sum account balances. The budget header now sums in **base currency** with conversion at each account's *latest known balance date* (i.e. today's spot for the live total). The per-account summary's recorded balance stays native (per-account). Where a cross-currency total is shown anywhere, an "approx" indicator appears next to it.

---

## Decision

### Schema (migration `0009_multi_currency.sql`)

Three new tables, no changes to existing tables:

```sql
CREATE TABLE setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE fx_rate (
    date   TEXT NOT NULL,                    -- ISO date 'YYYY-MM-DD'
    base   TEXT NOT NULL,                    -- ISO 4217, e.g. 'USD'
    quote  TEXT NOT NULL,                    -- ISO 4217, e.g. 'GBP'
    rate   REAL NOT NULL CHECK(rate > 0),    -- quote per 1 base
    source TEXT NOT NULL DEFAULT 'manual'
           CHECK(source IN ('manual','openexchangerates','derived')),
    PRIMARY KEY (date, base, quote)
);

CREATE INDEX idx_fx_rate_pair_date
    ON fx_rate(base, quote, date DESC);

CREATE TABLE transfer (
    iri              TEXT PRIMARY KEY,
    from_account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    to_account_id    INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    rate             REAL NOT NULL DEFAULT 1.0 CHECK(rate > 0),
    rate_source      TEXT NOT NULL DEFAULT 'derived'
                     CHECK(rate_source IN ('derived','manual','fx_rate')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_transfer_from ON transfer(from_account_id);
CREATE INDEX idx_transfer_to   ON transfer(to_account_id);
```

**Backfill** for existing transfer pairs (every transfer today is same-currency at rate 1.0):

```sql
INSERT INTO transfer (iri, from_account_id, to_account_id, rate, rate_source)
SELECT
    out_t.transfer_id            AS iri,
    out_t.account_id             AS from_account_id,
    in_t.account_id              AS to_account_id,
    1.0                          AS rate,
    'derived'                    AS rate_source
FROM      txn out_t
JOIN      txn in_t   ON in_t.transfer_id = out_t.transfer_id
                    AND in_t.id != out_t.id
                    AND in_t.amount > 0
WHERE out_t.amount < 0
  AND out_t.transfer_id IS NOT NULL;
```

**Seed defaults** for the settings the new code reads:

```sql
INSERT INTO setting (key, value) VALUES
    ('transfer_match_window_days', '3'),
    ('oxr_api_key',                ''),
    ('oxr_last_refresh_at',        '');
```

### Repository

**Settings:**

- `get_setting(key, default=None) -> Optional[str]`
- `set_setting(key, value)` — commits.

**FX rates:**

- `upsert_fx_rate(date, base, quote, rate, source='manual')` — INSERT … ON CONFLICT DO UPDATE.
- `get_fx_rate_on(date, base, quote) -> Optional[Decimal]` — exact-date lookup, returns None if missing.
- `get_fx_rate_nearest(date, base, quote) -> tuple[Optional[Decimal], Optional[str], bool]` — returns `(rate, rate_date_used, was_fallback)`. Tries:
  1. Exact bilateral `(date, base, quote)`.
  2. Exact USD-pivot — `(date, 'USD', quote) / (date, 'USD', base)`.
  3. Nearest-prior bilateral; sets `was_fallback=True`.
  4. Nearest-prior USD-pivot; sets `was_fallback=True`.
  5. Returns `(None, None, True)` when nothing exists.
- `convert_amount(amount: Decimal, *, from_ccy, to_ccy, on_date) -> tuple[Decimal, bool]` — returns `(converted, was_fallback)`. Same-currency returns `(amount, False)` cheaply.
- `list_distinct_currencies() -> list[str]` — every currency on any non-archived account (for the report selector).
- `list_known_rate_pairs() -> list[tuple[str,str]]` — every distinct `(base, quote)` we've ever stored a rate for, for the Currencies dialog.

**Transfer parent rows:**

- `create_transfer` and `convert_to_transfer` are amended to:
  - Also insert into / look up the `transfer` table.
  - Take optional `to_amount: Optional[Decimal] = None` and `rate: Optional[Decimal] = None` for the cross-currency path. The two-of-three rule: if `to_amount` is given, derive rate; if rate is given but `to_amount` is None, derive `to_amount = abs(from_amount) * rate`; if both are None and currencies match, use rate=1.0; if both are None and currencies differ, look up FX rate for that date and use it (with `rate_source='fx_rate'`).
  - The destination row's stored `txn.amount` is whatever the user (or FX lookup) determined for the receiving side — *not* a converted copy of the source amount. This is the key correctness rule: each account's ledger row matches what really hit that account's statement.
- `get_transfer(iri) -> Optional[TransferRow]` — for the cross-currency edit path.
- `update_transfer_rate(iri, *, rate, rate_source)` — for manual rate overrides.

**Account currency cache:**

- `account_currency(account_id) -> str` — small in-memory dict, invalidated on `create_account` / `update_account` / `delete_account`.

### FX fetcher (`mfl_desktop/fx.py`)

New module — no Qt imports, pure Python so the CLI can use it too.

- `OpenExchangeRatesClient(api_key)` — constructed once per refresh call.
- `client.fetch_latest(quotes: Iterable[str]) -> dict[str, Decimal]` — calls `latest.json`, returns the subset of quote currencies the caller asked for.
- `client.fetch_historical(date: str, quotes: Iterable[str]) -> dict[str, Decimal]` — calls `historical/YYYY-MM-DD.json`, same shape.
- `refresh_latest_into(repo, *, force=False)` — convenience: reads API key + last refresh time from `setting`, decides whether to fetch (skips if `<24h` and not `force`), calls `fetch_latest` for every quote in `repo.list_distinct_currencies()`, upserts rows. Returns a small result struct `RefreshResult(fetched_at, new_rates_count, errors)`.
- `backfill_historical(repo, *, base, quotes, date_from, date_to, on_progress=None) -> RefreshResult` — explicit historical fill, one call per missing date in the range.

Background refresh: `__main__` calls `refresh_latest_into(repo)` in a `QThreadPool` after the main window is up, so the launch path never blocks on network. Failures are logged and surface as a small "rates not refreshed" status-bar message — never a modal.

**API budget guard rails:**

- The "Backfill historical" verb shows the cost (`(date_to - date_from) days × number of pairs` API calls) before firing and asks for confirmation if the cost exceeds 100 calls.
- The launch refresh fetches `latest` once per session at most, regardless of how often the user re-opens the app.

### UI

**Manage → Currencies** (new dialog `mfl_desktop/ui/currencies_dialog.py`):

- API key field (with a small "Get a free key" hyperlink to openexchangerates.org/signup/free) plus a "this key is stored inside this file" disclaimer line.
- Last refresh time, "Refresh Now" button.
- Per-distinct-pair latest rate display.
- "Backfill historical…" verb opening a small date-range dialog.
- "Add manual rate" verb for one-off entries (USD↔ZWL for the dad joke case).

**Cross-currency transfer dialog** — when the New Transaction / inline / Bulk Edit transfer flow picks a destination whose currency differs from the source's:

- The standard destination-account prompt extends with a small panel: "Receiving in {to_ccy}: [____]" defaulting to `from_amount * looked_up_rate`, editable.
- The implied rate is displayed below the field for sanity: "Implied rate: 1 GBP = 1.2734 USD (openexchangerates 2026-06-05)."
- If no rate exists, the receiving amount field is empty and the user enters it; the implied-rate line reads "Manual rate."

**Report display-currency selector** — adds a small combo to each cross-account report:

- Spending Over Time
- Net Worth
- Budget header (tiles)
- Per-account summary's Top-N drill-down inside `TransactionsListWindow` (only when the drill-down spans accounts of different currencies, which only happens for the cross-currency reconcile flow; in the per-account case the window stays native)

Each report passes its display currency through to the new `Repository.convert_amount` helper at aggregation time. An "approx" badge appears in the report header when *any* conversion used a fallback rate.

### Files touched

| File | Change |
|---|---|
| `mfl_desktop/migrations/0009_multi_currency.sql` | New — schema + backfill + seed |
| `mfl_desktop/db/repository.py` | Settings, FX, transfer-parent surface; amend `create_transfer` / `convert_to_transfer` to support cross-currency and write the `transfer` row |
| `mfl_desktop/fx.py` | New — openexchangerates client + refresh helpers |
| `mfl_desktop/ui/currencies_dialog.py` | New — Manage → Currencies |
| `mfl_desktop/ui/register_window.py` | Cross-currency branch in transfer flow; Manage menu wires Currencies dialog; display-currency combo in reports menu glue |
| `mfl_desktop/ui/budget_window.py` | Tiles / chart use base-currency conversion when perimeter spans currencies |
| `mfl_desktop/ui/account_summary_window.py` | Native-currency render labelled (existing labelling already works); cross-currency aggregations in Top-N panels round-trip through `convert_amount` |
| `mfl_desktop/reports.py` | `convert_amount`-aware versions of the spending and net-worth aggregations |
| `mfl_desktop/budget_calc.py` | Accepts a converter callable for perimeter bucketing |
| `mfl_desktop/__main__.py` | Background launch refresh through `QThreadPool` |
| `mfl_desktop/cli.py` | `currencies refresh` subcommand for headless rate updates |

---

## Consequences

### Positive

- **No retrofit later.** Every aggregation path is currency-aware from day one. The first USD import doesn't break any report or cash badge.
- **txn-row truth-of-money preserved.** Each txn stores the amount that hit its account's statement, in that account's currency. No silent conversion at storage, no silent conversion at edit.
- **Transfer rate intent persists.** The `transfer` row records *what rate was used*, with provenance. Editing one side later doesn't silently rewrite the rate — the `transfer.rate` field is the truth-of-intent.
- **The Currencies dialog gives the user visibility.** Refresh time, last-fetched rates, manual entry — the rate state is never hidden from view.
- **openexchangerates.org integration is bounded.** Launch refresh is once-per-day, historical fill is explicit, the API-budget guard rails prevent accidental quota burns.
- **Single-currency users pay nothing.** The conversion path early-exits on same-currency lookups; the FX table stays empty until the first non-base account exists; no new pence-vs-decimal complexity.

### Negative / trade-offs

- **API key in the file.** Fine for single-user. The user is warned, and the disclaimer is on the dialog. A future "store in OS keychain" ADR is possible if real key-sharing becomes a thing.
- **Daily rates only.** A user who actually exchanges currency intraday at a non-spot rate (rare for personal finance) has to enter the rate manually for that transfer. Acceptable.
- **Backfill on demand, not automatic.** A user who has a year of USD txns will see "approx" badges on reports until they hit "Backfill historical." Acceptable; the alternative burns the OXR quota silently.
- **Budget perimeter denominated in base currency.** A user whose budget genuinely spans two home currencies (rare) has no per-budget currency override yet. Deferred.
- **`txn` doesn't gain a currency column.** A future "manual foreign-currency transaction on a domestic account" flow (e.g. a USD-denominated charge on a GBP credit card before the card converts) has no first-class shape. The workaround is to enter the txn at the converted GBP amount with a note in the memo. Tracked as a follow-up.

### Ongoing responsibilities

- **Every new aggregation that totals across accounts must run amounts through `convert_amount` on the way in.** The Repository's pure-Python report helpers are the right place to enforce this — adding a new report should mean adding a converter parameter, not adding a "TODO: support currency" comment.
- **`transfer.rate` is the single source of truth for intent**, not derived state. Any future "edit a transfer's rate" UI must write it through `update_transfer_rate`, not back-compute from amounts.
- **Provider rotation.** If openexchangerates.org disappears or the free tier changes, the `fx_rate.source` column is already shaped to host other providers. The fetcher module is the only swap point.
- **`setting` table discipline.** As preferences accrete in this table, the key namespace stays flat (e.g. `oxr_*`, `transfer_match_*`). Don't bury complex objects as JSON strings without a follow-up ADR — that's a sign the setting deserves its own table.
- **Background refresh failure mode.** Network errors must never block the launch path or interrupt the UI. The current pattern (status-bar text, no modal) is the contract.

### Out of scope here (covered separately)

- Transfer matching to existing transactions on the other side — **[ADR-036](ADR-036-transfer-matching.md)**.
- Bulk reconcile dialog for many candidate pairs at once — **[ADR-037](ADR-037-bulk-transfer-reconcile.md)**.
- Per-txn currency column for foreign-currency manual entries — future ADR if real use surfaces.
- Multi-currency budgets — future ADR.
