# ADR-177 — The investment reports default to the home currency

**Date:** 2026-07-22
**Status:** Implemented
**Related:** ADR-055 (the display-currency selector + FX conversion the other reports share). ADR-046 (the Investment Returns report). ADR-108 (the Investment Income report, which *specified* base currency and shipped without it). ADR-159 (Spending / Income convert to a display currency; the drop-and-report semantics for missing rates).

## Context

Owner-reported: *"why do the reports 'investment income' and 'investment returns' show in dollars, and only dollars? All other reports default to GBP and let me pick another currency."*

USD was never hardcoded. Both windows derived the display currency from the **selected accounts' own** currency:

```python
currencies = set(by_ccy)
self._display_ccy = (
    next(iter(currencies)) if len(currencies) == 1
    else accounts[0].currency
)
```

The owner's investment accounts are all USD, so `_display_ccy` became `"USD"` and every money cell rendered with `$`. This was a deliberate simplification at the time — `investment_returns_window.py`'s header said as much: *"the owner's portfolio is single-currency USD, so native is the live path."* Investment Income then shipped matching Returns rather than matching its own ADR: ADR-108 specified *base currency via the same ADR-055 FX path*, and the as-built note recorded the drift.

Five other report windows (Sankey, Spending, Income/Expense, Category/Payee, Net Worth) share a `_populate_ccy_combo` / `_on_ccy_changed` pair that reads `base_currency` from settings and `list_distinct_currencies()` for the options. Neither investment window had any of it: `_display_ccy` was a plain attribute recomputed each refresh, never user-settable. The FX plumbing was already present in both — `_conv` calls `Repository.convert_amount` with `_convert_missing` / `_convert_fallback` flags and a note line — but its **target** was the accounts' own currency, so it short-circuited and never actually converted a single-currency portfolio.

## Decision

**Adopt the same display-currency selector as the other five reports.** In each window:

- Add the canonical `_populate_ccy_combo` (base currency → else GBP → else first in use) and `_on_ccy_changed` (set `_display_ccy`, re-refresh) methods, verbatim from the shared pattern.
- Mount a `Display in:` `QComboBox` in the page header next to the existing Filter / Save actions.
- Delete the account-derived `_display_ccy` block. The value now comes from the selector; `_conv` already keys off `_display_ccy` and short-circuits for accounts already in that currency, so all downstream figures, the chart's `_sym`, and the missing-rate note follow with no further change.

The selector is a **view preference** — not persisted in the saved filters; it re-resolves to the default each time the report opens (as Net Worth's does under ADR-055).

Per-row **Price** and **Currency** columns stay native / informational — they describe the security, not the portfolio total, so they are deliberately left unconverted.

## Rejected

- **A per-report persisted currency in the saved filters.** The other five reports treat it as a transient view preference; matching them keeps one mental model. A saved Returns report reopens in the home currency, not whatever currency was last viewed.
- **Keeping native as the default and adding the selector on top.** That would leave the reported inconsistency in place (the owner's reports would still open in dollars) and only paper over it with a control. The ask was that they *default* to GBP.
- **Converting the Price / Ccy columns too.** They are informational native values (a US ETF's price is a USD price); converting them would misrepresent the instrument. Left native, as they were.

## Consequences

- **Both investment reports now open in the home currency (GBP) with a working currency selector**, consistent with every other report. Verified end-to-end against the demo file: Income defaults to GBP (first row `£765.09`; switch to USD → `$982.40`); Returns defaults to GBP (cost `£75,757.14`; switch to USD → `$97,274.19`). Price / Ccy stay native in both views.
- The `investment_returns_window` module docstring's claim that *"native is the live path"* is now false and was rewritten; Income's docstring was brought back in line with what ADR-108 originally specified.
- **A latent issue is now on the live path, and is deliberately left for a follow-up.** `_conv` handles a missing FX rate by returning the amount *unconverted* — i.e. counted at face value, $1 = £1, with a note flagging it. That was harmless while conversion never ran on a single-currency portfolio; now that GBP is the default target, any date with no rate on file adds dollars to pounds 1:1. This is the exact failure ADR-159 fixed for Spending / Income by **dropping** no-rate slices and reporting them in `unconverted`. This ADR does **not** change the investment aggregation semantics — that is a larger decision (drop-and-report vs. backfill the rates first) surfaced to the owner separately. Recorded here so the next reader knows it was seen, not missed.

Full suite 423 passed, 0 failed. No schema change.
