# ADR-156 — Spending / Income Over Time convert to a display currency

**Date:** 2026-07-12
**Status:** Implemented
**Related:** ADR-055 (display-currency conversion; the non-persisted "Display in" view preference). ADR-026 (the hand-rolled stacked-bar chart). ADR-088 (Income Over Time as the mirror of Spending Over Time). ADR-129 (net expense — refunds reduce a category's spend). ADR-089/110 (reinvested-dividend income). ADR-154 (bar totals — which added a *fourth* place the hard-coded `£` appeared).

## Context

ADR-154 flagged, in passing, that `SpendingChart` hard-codes `£`: `fmt_currency`'s default symbol is used for the y-axis, the average pill, the tooltip, and (newly) the bar totals. It was filed as a cosmetic bug for non-UK users.

It is not cosmetic. Pulling the thread found that **`spending_aggregates` and `income_aggregates` had no currency awareness at all**:

```sql
SELECT bucket, t.category_id, SUM(-t.amount) AS spending_pence
FROM txn_category_line t JOIN category c ON c.id = t.category_id
...
GROUP BY bucket, t.category_id
```

Amounts are stored in **each account's own currency**. That `SUM` therefore adds dollars to pounds **1:1**, and the chart then stamps a `£` on the result. The number is wrong, not just the label.

This is not a hypothetical. The owner's live file is:

- **25 USD accounts, 13 GBP accounts**
- **34,648 USD transactions** vs 2,684 GBP
- `base_currency` unset

Measured against it, the 2025 income total read **416,906** where the true figure is **325,410 GBP** — overstated by ~28%. The owner has been reading these numbers. The "Total: £294,993.45" on the Passive Income report screenshot is dollars and pounds added together.

Every *other* report already got this right — the Sankey, the Payee report, Income & Expense and Net Worth all take a `display_currency`, `GROUP BY ... a.currency`, and convert each group via `convert_amount`. Spending/Income Over Time were simply never brought along. Notably the Sankey screenshot that started this thread looked fine precisely because it was filtered to a single GBP account.

## Decision

**Bring these two reports onto the same footing as every other report.**

### 1. The aggregates convert

`spending_aggregates` / `income_aggregates` (and the reinvested-dividend pass behind `include_reinvested`) gain `display_currency`, join `account`, `GROUP BY ... a.currency`, and convert each per-currency group at `date_to`. A new `Repository._to_display_ccy()` holds the rule once — it is the same rule `sankey_category_totals` and `payee_spending_aggregates` already inline, now named.

**Convert first, then net, then clamp.** ADR-129 nets a category's refunds against its spend across the whole bucket. Netting per-currency *before* converting would clamp a currency's refund away (a net-negative group drops) before it could offset that same category's spend in another currency, and the total would come out too high. The order is load-bearing and is pinned by a test.

A single-currency file never touches the FX tables: `_to_display_ccy` returns the amount unchanged when the currencies match, so the common case is byte-identical to before.

### 2. Return shape carries what couldn't be converted

Both now return `{"rows": [...], "unconverted": {ccy: pence}}` — matching `payee_spending_aggregates`.

An amount with **no rate on file is dropped, not counted at face value**, and the magnitude is recorded. The report surfaces it in the summary panel ("⚠ Not converted (no GBP rate on file): USD 1,234.00"). This matters: a silently understated total would be a *quieter* version of the very bug being fixed, and quieter is worse. Better to be visibly incomplete than invisibly wrong.

### 3. The window picks the currency; the chart is told the symbol

`SpendingReportWindow` gains the same **"Display in"** combo the Sankey and Net Worth already have — a view preference, not part of the saved filters, re-resolving to the default (base currency → GBP → first in use) each time the report opens. `SpendingChart.render()` takes `currency_symbol`, and the four format sites use it.

`chart_helpers.currency_symbol()` is the single definition of the glyph (it was duplicated in `sankey_report_window` and `home_view`), returning the code plus a space for a currency we have no glyph for — `"CHF 1,234"` — so a number is never ambiguous. `fmt_currency`'s `£` default is now documented as a convenience that a caller with a display currency **must** override; relying on it is what caused this.

### Rejected

- **Just pass the symbol through (the ADR-154 reading).** Fixes the glyph and leaves the arithmetic broken — the worst outcome, because a correctly-labelled wrong number is more believable than an obviously-wrong one.
- **Convert at each transaction's own date** rather than at `date_to`. Arguably more "correct" for a historical series, but it disagrees with every other report in the app, and an inconsistency between two reports showing the same money is worse than a defensible convention applied uniformly. If we revisit this, it should change everywhere at once.
- **Count unconvertible amounts at face value** (1:1) rather than dropping them. That *is* the bug.
- **Force a base currency at first run.** Worth doing on its own merits (the owner's file has none), but it wouldn't fix this — the aggregates ignored currency entirely, base or not.

## Consequences

- Spending Over Time and Income Over Time now report real money. On the owner's file the numbers move materially — 2025 income from 416,906 to **325,410 GBP** — and the previously-displayed figures should be treated as wrong.
- Both reports gain a "Display in" selector, so the owner can read the same data in USD (where 93% of their transactions live) or GBP.
- A single-currency file is unaffected: same numbers, same `£`, no FX lookups.
- Unconvertible money is visible rather than silently missing.
- `spending_aggregates` / `income_aggregates` changed return type from `list` to `dict` — two call sites (the window, one test) updated. They are internal.
- **Still hard-coded elsewhere:** `home_view` and `sankey_report_window` each keep a private copy of the currency-symbol map. They are correct today; they should collapse onto `chart_helpers.currency_symbol()` next time either is touched. Not done here to keep this change to the bug.
- 10 new tests (`tests/test_spending_income_display_currency.py`), all 10 of which fail against the pre-ADR code — including one that asserts the *old* raw-sum behaviour really did produce the wrong number, so the test can't quietly stop exercising the regression, and one that pins the convert-then-net order. Full suite 263/263. No schema change.
