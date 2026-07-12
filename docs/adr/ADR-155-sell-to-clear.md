# ADR-155 — Sell to clear: a closing sale lands the position on exactly zero

**Date:** 2026-07-12
**Status:** Implemented
**Related:** ADR-044 (the holdings engine; FIFO lots). ADR-053 (ShrsIn/ShrsOut are custodian moves, not disposals). ADR-043 (`txn.amount` is the signed cash impact; `security.archived_at` is in the schema from here). ADR-093 (the tri-field qty ⇄ price ⇄ total solver + per-instrument multiplier). ADR-154 (the same solver extended to a reinvest). ADR-049 (Tiingo request waste — one request per tracked ticker, every sweep). ADR-148 (the last data repair of this shape).

## Context

Owner: *"When a security is cleared from an account, the holdings should go to zero with no lingering rounding errors… quite a lot of the fractionals are just because share roundings build up over years, and when the final sale happens, they either oversell or undersell by some fractional."*

The diagnosis is sharper than the description, and it is not rounding drift.

**The engine clamps an oversell and says nothing.** `compute_holdings_view` drains FIFO lots with `while remaining > _EPS and queue:` — when a sell exceeds the shares held, the loop stops because the queue is empty and **the unmatched remainder is silently discarded**. No exception, no warning; a `logger.info` at most. Two things follow:

1. **Realised gain is overstated.** The unbacked shares are sold with zero cost matched against them, so their entire proceeds book as gain.
2. **Worse: the data goes negative where the engine goes to zero.** Every subsequent share-in for that security then sits on top of a floor the engine never applied.

That second point is what produces the owner's "fractionals". A broker (or an importer) that has oversold will emit a compensating **`ShrsIn` plug** to force its own running total back to zero. But the engine already clamped, so the plug doesn't cancel anything — it **materialises shares out of nothing**, with no cost basis. The residual isn't a sliver you still own; it's an artifact.

Measured on the owner's file (39,854 transactions, 9 investment accounts), **12 oversells**, four of which are still showing as holdings today:

| Account | Security | Phantom | Value | What happened |
|---|---|---|---|---|
| MS Access - Mark | SUSA | 7.000 sh | £1,090 | Sold 56 on 2021-01-05 holding 49 |
| MS Access - Maria | DSI | 1.569 sh | £224 | `ShrsOut` 91.019 holding 89.450 |
| eTrade Mark Hall | VWID | 0.403 sh | £15.51 | Sold **1167** holding **1166.597** |
| eTrade Mark Hall | PDBC | 0.002 sh | £0.03 | Sold 1251.501 holding 1251.5, then *two* `ShrsIn 0.001` plugs |

VWID is the whole story in one row: the broker sold "everything" and wrote the round number **1167** against a holding of **1166.597**.

A secondary irritation, same root: a closed-out security keeps being priced. `securities_to_price` skips *orphans* (no transactions), not *fully-exited* positions — so 54 securities the owner no longer holds still occupy the Securities screen, and the ones with tickers still spend a Tiingo request on every sweep (ADR-049's scarce budget).

## Decision

**A Sell can be marked "Sell to clear", and it sells the exact holding — the engine's own figure, not a number typed off a statement.**

### The dialog

- A **"Sell to clear — close the position out"** checkbox, Sell-only. The stored `txn.action` stays `Sell`: the holdings engine, the QIF importer/exporter, and every report that switches on action are all untouched. This is the whole reason it is a checkbox and not a new action kind.
- Ticking it fills the quantity from `holdings.shares_held(...)` and makes it **read-only**; the **Proceeds** field (the same tri-field total from ADR-093/154, relabelled) becomes what you type, and the price is backed out of it. That is the right way round for a closure: the statement states the *cash*, and the share count is whatever the account happened to hold.
- With the quantity pinned it is no longer a solve target. Editing the price re-solves the **proceeds** instead — otherwise the solver would cheerfully overwrite the holding we exist to clear.
- On save the quantity is re-read from the engine and stored as `Decimal(repr(held))`, **not** parsed back from the display field. The displayed figure is trimmed for readability, and selling a *rounded* quantity is precisely the bug. Full float precision drains the lots to nothing.

`shares_held` is scoped by **date** (a closure entered late sells what you held *then*, not what a later buy added) and **excludes the row being edited** (re-opening a saved close-out must not read its own shares as still held, or each save would halve the quantity).

### After the close

The security is offered for retirement — **asked, not assumed**, because the same security can be held in several accounts and closing one is no reason to stop pricing the others. The prompt only appears when *no* account holds it any more.

Retiring writes **`security.archived_at`** — a column that has been in the schema since ADR-043, that **every read path already filters on**, and that **nothing has ever written**. So the gate was fully built and simply never wired: `securities_to_price` (the security stops costing a Tiingo request), `securities_missing_history`, `securities_with_incomplete_history`, `list_securities`. It is a display/pricing gate only — transactions, prices and realised gain are untouched, which is what makes it losslessly reversible via **"Track again"** on the Securities screen (behind a new *Show retired* toggle, so retiring can't become a one-way trap).

Rejected:

- **A distinct `SellAll` action kind.** It would need teaching to the holdings engine, the QIF parser and writer, `qif_actions`' predicate sets, and every report that classifies by action — a wide blast radius for what is a data-entry aid. The stored row is an ordinary Sell; only the *way its quantity is derived* is new.
- **Auto-zeroing the residual with a corrective `ShrsOut`.** This is what the broker's plug already tries to do, and it's how the phantoms got here. Adding rows to cancel rows compounds the fiction; the fix is to not oversell in the first place.
- **Making an oversell a hard error in the engine.** Tempting — the silent clamp is the root defect — but 12 rows in the live file would start raising, and `compute_holdings_view` is on the launch path. The clamp is left as-is (it is at least *safe*), the data is repaired, and the dialog stops producing new ones. Revisit as a validation warning.
- **Auto-archiving on close.** Fine for a single-account holding, wrong the moment the security is held elsewhere; and silently retiring something the owner may want to keep watching is a decision that belongs to them.

### The repair (`tools/repair_share_oversells.py`)

Operator tool, not shipped. For a **rounding** oversell (≤ 0.5 shares — a broker rounding a "sell everything") the truth is that the sale sold everything and no more, so it **trims the sale's quantity to what was held and deletes the compensating plug(s)**. The **cash amount is never touched** — that came off the statement — which is exactly what makes the realised gain correct: the full basis now matches the true proceeds.

It collects *every* plug, not the first: PDBC carries **two** `ShrsIn 0.001` rows a year apart (the same fractional adjustment imported from two statements), and a half-repair leaves the position off zero.

A **large** oversell is not rounding — it means history is missing, and trimming the sale to fit would rewrite the statement to match a gap in the data. Those are reported and skipped unless named with `--add-shares`, which inserts the shortfall as a `ShrsIn` **before** the sale, leaving the sale exactly as the broker recorded it.

Dry-run by default; snapshots the file before writing; and **refuses to commit unless every repaired position replays to exactly zero**.

## Consequences

- A closure entered through the dialog can no longer over- or under-sell. The position leaves the Holdings table, the Securities screen, and (if retired) the price sweep.
- Applied to the owner's file: **PDBC, VWID and DSI phantoms gone**; portfolio value falls by **£239.75**, which is exactly `0.03 + 15.51 + 224.21` — the phantoms and nothing else. Realised gain on the six trimmed sales is now backed by their full cost basis.
- **SUSA (7 shares, £1,090) is deliberately left alone.** The 2021-01-05 sale of 56 shares against 49 held is too large to be rounding: the file is missing a 7-share purchase, and the sale itself matches the statement. Fixing it needs the 2020 statement, not a guess. It will keep showing as a holding until then — that is the honest state, and the tool names it every run.
- Two 2010 `ShrsOut` rows in MS 401(k) (10.465 and 11.933 shares out of a zero holding), a 2012 Dodge & Cox sale (0.979) and a 2026 MS Pension sale (0.551) are likewise reported and skipped. They leave no phantom; they only make those securities' realised gain approximate.
- `security.archived_at` finally has a writer, so the ADR-049 request budget stops being spent on securities the owner exited years ago.
- The engine's silent oversell clamp **still exists**. This ADR removes the ways it gets fed and repairs what it produced; it does not make overselling impossible. A future validation pass should warn at entry and at import.
- Covered by `tests/test_investment_sell_to_clear.py` (12 tests: the exact quantity vs a rounded one, the position gone from the view, realised gain = proceeds − whole basis, the locked quantity, price edits retargeting onto proceeds, unticking, date-scoping, the edit-reopen halving trap, Sell-only visibility, refusing to clear nothing, and archive/restore round-trip). The close-out ends in a modal prompt, which offscreen Qt still *blocks* on — the tests stub it, or they hang rather than fail.
