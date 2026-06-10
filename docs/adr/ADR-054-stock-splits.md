# ADR-054 — Stock splits adjust the FIFO lots

**Date:** 2026-06-10
**Status:** Accepted
**Related:** ADR-044 (FIFO holdings engine; splits were *deferred* there — "Stock-split ratio application is deferred — splits are skipped with a logged note"), ADR-046 (Investment Returns — where the symptom showed), ADR-048 (investment transaction dialog — gains the split entry), ADR-053 (the transfer fix shipped just before; splits sit in the same replay alongside ShrsIn/ShrsOut).

---

## Context

ADR-044 deferred stock splits: a `StkSplit` action was skipped (logged + the holding flagged `basis_incomplete`), on the basis that the owner's only split was a malformed empty row. Real data caught up with us — **HDV (iShares Core High Dividend ETF) had a 5-for-1 split**. Tiingo stores the actual traded prices, so HDV's series steps from ~$140 (pre-split) to ~$27 (post-split). With the share count never scaled, the holdings engine valued the **pre-split** 214.779 shares at the **post-split** $27.54 price:

```
214.779 sh × $27.54 = $5,915 market value  vs  $21,655 cost  →  a bogus −72.7% "loss"
```

It surfaced in the new best/worst performers panel (HDV as the worst performer) and equally corrupted the Stock Record position, the Returns report's Unrealized column, and the value-over-time chart — anything reading shares × price.

A second wrinkle: the split wasn't recorded as a `StkSplit` at all. In the owner's data it arrived as an **`XIn` of 859.116 shares at $0** on 2026-04-30 — and 214.779 × 4 = 859.116, i.e. the *extra* shares a 5-for-1 split creates (214.779 × 5 = 1,073.895). The engine ignores `XIn` (whole-account transfers, deferred to round 4), so those shares vanished.

## Options considered

**(A) Adjust stored prices instead of shares** (rewrite the price history to be split-adjusted). Rejected: Tiingo's real prices are correct as-is; rewriting them fights the data feed (every refresh would re-introduce raw prices), and historical *traded* prices are worth keeping. The share count is what's wrong, so fix the shares.

**(B) Auto-detect an `XIn @ $0` as a split.** Rejected: `XIn` legitimately means a whole-account transfer-in (shares with a real, if unknown, basis). Silently reinterpreting it as a split is a hack that would misfire on genuine transfers.

**(C) Model a split as a `StkSplit` action that scales the FIFO lots; the ratio lives in the quantity field.** Chosen. On a `StkSplit` with ratio `r` (new shares per old), every open lot's share count is multiplied by `r` and its per-share cost divided by `r` — so **total cost basis is unchanged** and, because the shares scale up exactly when the price scales down, **market value stays continuous** across the split. This is the standard cost-basis treatment and needs no price rewrite.

**(D) Keep deferring.** Rejected — real holdings are now wrong in four places.

## Decision

- **Ratio convention:** `txn.quantity` on a `StkSplit` row = **new shares per old** — `5` for a 5-for-1, `0.1` for a reverse 1-for-10. `price` is NULL, `amount` is 0 (no cash, ADR-043 invariant holds).
- **Engine (`holdings.py`):** a shared `_apply_split(queue, ratio)` scales every open lot (`qty *= ratio`, `unit_cost /= ratio`; no-op for `ratio <= 0` or ~1.0). Wired into all three replays — `compute_holdings_view`, `compute_returns`, `compute_value_history` — so the position screen, the returns report, and the value chart agree. A split with no ratio (the old malformed case) is still skipped + flagged `basis_incomplete`.
- **Entry (`investment_transaction_dialog.py`):** a new **"Stock split"** action shows a single **"Split ratio"** field (and the security), hiding qty/price/amount; on save it stores `action='StkSplit'`, `quantity=ratio`, `price=NULL`, `amount=0`. Edit mode seeds the ratio field from the stored quantity. Reverse splits are entered as a fraction.
- **The owner's HDV row:** convert the existing `XIn 859.116 @ $0` to a `StkSplit` of ratio 5 (open it in the register, change Action → Stock split, ratio 5, save). That single edit yields the correct position.

## Consequences

- **HDV is correct after the conversion** — verified on the live DB: 214.779 → **1,073.895 shares**, cost basis unchanged at **$21,655.29**, avg cost **$20.17** (= $100.83 ÷ 5), market value **$29,575**, unrealized **+$7,920** (was −$15,740). The value-over-time chart stays continuous through the split date (shares step up as price steps down).
- **General feature, not a one-off** — any future split is a first-class entry, and reverse splits work via a fractional ratio.
- **Prices are never rewritten** — the split adjusts shares/basis only; Tiingo's real traded prices stay intact, so a refresh can't undo it.
- **Existing `XIn`-as-split rows must be converted** by hand (only HDV today). A split that straddles an in-kind transfer (ADR-053 pen) isn't specially reconciled — rare; the lots are scaled wherever they currently sit.
- **`basis_incomplete` still surfaces** for unrelated reasons (HDV also has a 1.634-share `ShrsIn` with no price), which is honest — the split itself no longer flags it.
- **Verified** headless on a WAL-consistent snapshot (HDV figures above) + offscreen Qt (the dialog shows the ratio field for a split, hides qty/price/amount, saves `StkSplit`/qty=ratio/price=NULL/amount=0, and seeds the ratio in edit mode).
