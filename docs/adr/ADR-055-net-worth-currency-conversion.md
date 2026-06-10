# ADR-055 — Net Worth converts to a display currency

**Date:** 2026-06-10
**Status:** Accepted
**Related:** ADR-019 (Net Worth report — the screen being fixed), ADR-035 (multi-currency foundation — `fx_rate`, `convert_amount`, `get_fx_rate_nearest`, the Currencies dialog; its §UI specified a display-currency selector that was never wired in), ADR-044 (`compute_account_values` — the per-account market values, returned in each account's native currency), ADR-046 (Investment Returns `_conv` — the convert-and-flag pattern this mirrors, but with a different missing-rate policy).

---

## Context

The Net Worth headline was materially wrong. The live file has **7 GBP + 7 USD accounts**, person base currency GBP, and **0 `fx_rate` rows**. `NetWorthWindow._refresh` sums `compute_account_values()` — which returns each account's value **in its own currency** — as bare pence with **no conversion**, and labels the total "£". So the ~$300k USD brokerage was added to the GBP accounts **at par** ($1 = £1), overstating net worth by roughly a third.

The fix is purely wiring: ADR-035 already shipped `Repository.convert_amount(amount, from_ccy, to_ccy, on_date) -> (Decimal|None, was_fallback)` (a six-step nearest-rate lookup), the `fx_rate` store, openexchangerates refresh, and a Currencies dialog with manual-rate entry. None of it was plumbed into Net Worth (or the sidebar folder roll-up — see Consequences). The Investment Returns report (ADR-046) already established the convert-and-flag pattern with `_conv`.

## Options considered

**Missing-rate behaviour** — the crux, because the file has no rates yet.

**(A) Fall back to the unconverted amount (1:1) + a warning, like ADR-046's `_conv`.** Rejected for Net Worth: 1:1 *is* the bug. Re-introducing a par-add behind a warning still shows a wrong headline that looks authoritative; the returns report can tolerate it because its totals are returns, not a balance you'd quote.

**(B) Exclude unconvertable accounts from the totals and flag prominently.** Chosen. A needed rate that's missing means we *cannot* honestly fold that account into the headline, so it's left out and a banner states the excluded amount **in its native currency** ("7 USD account(s) — $312,481 — excluded; no USD→GBP rate") with a **"Set exchange rate…"** button that opens the Currencies dialog. The number shown is always either correct or visibly incomplete — never silently wrong.

**(C) Block the whole report until rates exist.** Rejected: a single-currency user, or one mid-setup, still wants their (correct) partial number; blocking is heavier than flagging.

**(D) Auto-fetch the rate on open.** Rejected as the default: it needs an OXR API key + network and would surprise. The "Set exchange rate…" button routes to the Currencies dialog where the user can *either* add a manual rate *or* Refresh Now — an explicit, one-click path instead of hidden I/O.

**Display target** — convert to the person's base currency, with a **selector** (ADR-035 §UI) defaulting to it (`get_setting("base_currency")`, else GBP if present, else the first currency in use). The selector is cheap given `list_distinct_currencies()` already exists, and lets a USD-minded user flip the whole report to `$`.

**Per-account rows** — shown **converted** to the display currency (so a column reads coherently in one unit) with the **native amount in the tooltip**; an unconvertable leaf shows its native value + "(no rate)" rather than a fabricated converted one.

## Decision

`NetWorthWindow` gains a top bar with a **Display currency** combo (default = base currency) and, when needed, a **missing-rate banner** + "Set exchange rate…" button. On refresh it builds `converted[account_id] -> Decimal | None` by calling `convert_amount(native_value, from_ccy=account.currency, to_ccy=display_ccy, on_date=today)` for every account; same-currency is identity. Every total — per-type, per-family, Assets, Debts, and the net headline — sums the **converted** values, skipping `None`. Accounts whose conversion returned `None` (and are non-zero) are collected into the banner. A nearest-prior (fallback) rate sets a softer "rates as of an earlier date" note. All money formatting takes the display currency's symbol (no more hardcoded `£`). Conversion date is today (net worth is an as-of-now snapshot; the lookup falls back to the nearest prior rate).

## Consequences

- **The headline is now either correct or visibly incomplete.** With a USD→GBP rate on file the total is right; without one, the USD accounts are excluded and the banner says so in dollars, with a one-click fix. No silent par-add.
- **Guided first-run.** Because there are 0 rates today, the first open flags all USD accounts; the user clicks "Set exchange rate…", adds (or fetches) USD→GBP once, and the headline completes.
- **Display-currency selector** lets the report be read in any in-use currency.
- **Sidebar folder roll-up has the same class of bug** (`sidebar.py` sums folder members across currencies) but already degrades to showing *no* currency symbol on a mixed folder, so it misleads less. It is **left as a noted follow-up** to keep this change scoped to the headline report; the same `convert_amount` plumbing applies when it's done.
- **Per-account truth is preserved** via native-amount tooltips; only the aggregates move to the display currency.
- **Verified** headless on the live DB: with 0 rates the GBP total is correct and the USD accounts are reported excluded with their dollar sum; after inserting a manual USD→GBP rate the net worth folds the brokerage in at the converted value and the banner clears.
