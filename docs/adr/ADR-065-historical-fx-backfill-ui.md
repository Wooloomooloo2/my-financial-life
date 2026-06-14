# ADR-065 — Historical FX rate backfill (UI + sampling granularity)

**Date:** 2026-06-14
**Status:** Accepted
**Related:** ADR-035 (multi-currency foundation — the `fx_rate` table, `OpenExchangeRatesClient`, `get_fx_rate_nearest`, and the `backfill_historical` backend this finally wires to a UI; ADR-035 §guard-rails called for an API-cost confirmation). ADR-055 / ADR-056 / ADR-064 (Net Worth, Sankey, Income & Expense — the reports that convert via the FX layer and so depend on rates existing for historic dates). ADR-049 (Tiingo request-waste reduction — the analogous "don't burn the quota" discipline for securities prices). ADR-037 (transfer reconcile — cross-currency matching needs a rate for the transfer date).

---

## Context

The multi-currency layer (ADR-035) shipped with `fx.backfill_historical` already written — it fetches openexchangerates' `/historical/YYYY-MM-DD.json` for a date range — but **nothing ever called it.** The Currencies dialog only had *Refresh now* (today's rates) and *Add manual rate*. So the only way to get a historic rate was to type it in by hand, one row at a time.

This bit in practice: the owner's `fx_rate` table held rates only from June 2026, while their data has cross-currency activity going back years (e.g. a USD→GBP house-funding sequence in October 2025). Every report that converts (Net Worth, Sankey, Income & Expense) and the cross-currency transfer matcher rely on `get_fx_rate_nearest`, which is **nearest-*prior*** — so a 2025 transaction with no rate on or before its date gets no conversion at all (excluded from totals; skipped by the matcher). The cross-currency reconcile found **0 candidates** purely because no historic rate existed; adding one rate surfaced all 11 as Strong matches.

The constraint is cost: openexchangerates' free tier is ~1,000 requests/month and `backfill_historical` makes **one call per fetched date**. A multi-year *daily* backfill would blow the budget. Put to the owner via `AskUserQuestion`: **Monthly sampling by default, with Weekly/Daily selectable**, and a live "≈ N API calls" estimate before running. (Rejected: daily-only — precise but quota-hungry; monthly-only — cheapest but inflexible.)

---

## Decision

Add a **"Backfill historical…"** button to the Currencies dialog, opening a new `FxBackfillDialog` (`mfl_desktop/ui/fx_backfill_dialog.py`):

- **Date From / To** (From defaults to the earliest transaction date so a backfill covers all history, falling back to 5 years ago; To defaults to today).
- **Sample granularity** — Monthly (default) / Weekly / Daily.
- A **live "≈ N API calls (one per sampled date)" estimate** that recomputes on any change, naming the currencies it will fetch.
- Runs synchronously behind a `QProgressDialog`; **cancel stops after the current date** (rates already fetched are kept). A **double-confirm** fires when the sample count is large (≥ 60), restating the free-tier budget per ADR-035 §guard-rails.

**Backend (`fx.py`).** `backfill_historical` gains a `granularity` parameter; a new pure `sample_backfill_dates(date_from, date_to, granularity)` enumerates the dates to fetch — daily (every day), weekly (every 7 days from the start), or monthly (the start date, then the 1st of each following month). `date_to` is always appended as the final sample so the most recent edge gets a rate. The fetch loop iterates the sampled dates instead of every day, keeping the existing **skip-if-already-present** check (re-running is cheap and idempotent) and per-date error accumulation. USD-base only (free-tier constraint), so the quotes are the **non-USD currencies in use**; a USD-only file disables the button.

**Why monthly is enough.** `get_fx_rate_nearest` resolves any date to the nearest-prior stored rate, so one rate per month covers every day in that month for conversion and for the ±tolerance transfer matcher. Personal-finance reporting doesn't need per-day FX precision; the linked transfer's *stored* rate is back-derived from the two real amounts anyway (ADR-035), so the sampled rate only drives display conversion and match confidence, never the recorded transfer.

---

## Options considered

- **Monthly default + selectable (chosen)** vs. daily-only vs. monthly-only — see Context. The selectable granularity lets the owner trade precision for quota per run; monthly default keeps the common case cheap.
- **Sampling in the backend (chosen)** vs. in the dialog — `sample_backfill_dates` is pure and unit-tested, and `backfill_historical` stays the single fetch path; the dialog only picks parameters and shows the estimate.
- **Synchronous run + `QProgressDialog` (chosen)** vs. a background thread — matches the existing synchronous *Refresh now* pattern; monthly runs are short, and cancel-after-current-date covers the long-range case. (Cancel uses a `KeyboardInterrupt` from the progress callback, which escapes `backfill_historical`'s `except Exception` cleanly.)
- **Append `date_to` as a final sample (chosen)** vs. strict stepping — guarantees the newest edge gets a rate without waiting for the next monthly step; duplicates are absorbed by skip-if-present.

---

## Consequences

### Positive
- Historic rates are now obtainable from the UI — unblocking correct multi-currency conversion in every report and the cross-currency transfer matcher for past dates.
- The API-cost trade-off is explicit and controllable (granularity + live estimate + large-run confirm), so the free-tier budget isn't burned by accident.
- Idempotent + skip-existing means re-running to "top up" is cheap; pure `sample_backfill_dates` is fully tested.

### Negative / trade-offs
- **Monthly sampling is an approximation** — intra-month rate moves aren't captured. Fine for personal-finance reporting and harmless for transfer matching (real amounts drive the booked rate); switch to Weekly/Daily for a short range if precision matters.
- **Still one API call per sampled date** — a very long daily backfill can exceed the free tier; the estimate + confirm make that visible, but there's no hard quota check (OXR usage isn't queried up front).
- **Cancel is coarse** (stops after the current date, not instant) — acceptable given per-call latency.

### Ongoing responsibilities
- **A new granularity** means extending both `BACKFILL_GRANULARITIES` and `sample_backfill_dates` together.
- If a paid OXR plan or a different provider is ever adopted, revisit the monthly default — daily becomes affordable and the sampling rationale changes.
