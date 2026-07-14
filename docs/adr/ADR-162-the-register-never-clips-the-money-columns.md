# ADR-162 — The register never clips Amount and Balance

**Date:** 2026-07-13
**Status:** Implemented
**Related:** ADR-161 (the design review this came out of). ADR-061 (register model + proxy). ADR-043 (the investment register's extra columns). ADR-113 (the screenshot harness). ADR-034 (`budget_drilldown_window`, which already stretched its Memo column — the precedent).

## Context

From the same design review as ADR-161, but this one is a **defect, not cosmetics**.

`RegisterWindow` sized its columns from a fixed pixel map and turned off `stretchLastSection`. Measured against the app's default 1320 px window:

| mode | columns demand | viewport | overflow |
|---|---|---|---|
| account register | 1,160 px | 967 px | **+193 px** |
| all transactions | 1,210 px | 967 px | **+243 px** |

Amount and Balance are the **last two columns**, so all of that overflow landed on them. The result: a ledger whose two headline numbers — what the transaction was for, and what you had afterwards — sat behind a horizontal scrollbar, while **Memo, which is empty on most rows, held a fixed 280 px** of the space they needed.

All Transactions was the worse case: it adds an Account column and drops Balance, so 243 px of overflow put **Amount entirely off-screen**.

This is the kind of thing that only shows up when you look at the app. The code reads fine.

## Decision

**Memo is the column that flexes; the money columns are the ones that don't.**

Memo is free text, empty on most rows, and already elides — it is the one column that can afford to give up width. So `memo` becomes a `QHeaderView.Stretch` section (absorbing whatever the viewport leaves over), every other column keeps a fixed, user-draggable `Interactive` width, and the fixed widths are trimmed so that **everything except Memo fits the viewport**. `budget_drilldown_window` already did exactly this; the register simply never adopted it.

**Order is load-bearing, and this nearly shipped wrong.** `setColumnWidth` is *silently ignored* on a section that is still in `Stretch` mode. Memo sits at a different column index in each register mode, so switching modes re-applied widths while the old Stretch section was still stretched — leaving whichever column now occupied that index (Quantity, say) pinned at Memo's inherited width, and the grid overflowed again. The fix is to **reset every section to `Interactive`, then apply widths, then stretch Memo** — in that order. There is a test for precisely this.

## Rejected

- **`setStretchLastSection(True)`** — the last section *is* Balance. That stretches the very column we are trying to protect and leaves Memo fixed. Exactly backwards.
- **Frozen/pinned money columns** (a second overlaid view, the standard Qt recipe). It would make the guarantee absolute at any width, including the investment register. It is also a real chunk of machinery — a second `QTableView`, synchronised scroll, selection and sort — for a problem that trimming widths solves for the two registers people actually live in. Revisit if the investment register's limitation below starts to bite.
- **Making Payee and Category stretch as well.** Space would then be split three ways, and a `Stretch` section cannot be dragged — so the user would lose the ability to resize the two columns they are most likely to *want* wider. Memo alone keeps the cost to the column nobody widens.

## Consequences

- The account register and All Transactions fit their viewport at the default window size and at every larger one. Amount and Balance are always on screen.
- **Memo can no longer be resized by dragging** — it is a Stretch section now. Accepted: it takes whatever is left, which is what it was for.
- Default widths are narrower across the board (Payee 220 → 190, Category 200 → 170, Memo 280 → 180 as a starting width, etc.). Nothing is hidden; the text elides as it always did.

## Known limitation — the investment register

**The investment register still scrolls in a small window, and this ADR does not fix that.**

It carries **eleven** columns (the cash set plus Action / Symbol / Security / Quantity / Price). Their fixed widths total ~1,090 px against a 967 px viewport — so even with Memo squeezed to nothing, *the columns that are not Memo already do not fit*. No amount of stretching solves that: eleven columns do not fit 967 px at any honest width.

What this ADR does do is trim them so the grid fits from a viewport of ~1,090 px — a maximised window on any modern display — and pin that boundary with a test. Below it, Amount and Balance can still scroll off. The real fixes are frozen money columns (rejected above) or dropping a column from that mode, and both are bigger decisions than this one. **Stated here rather than left to be rediscovered.**

## Verification

`tests/test_register_money_columns.py` 5/5 — **all five fail against the unfixed code**, which is the point.

One thing worth recording, because it wasted a cycle and would waste the next person's: **under `QT_QPA_PLATFORM=offscreen`, a top-level window ignores `resize()`.** The register came back with a 1,607 px viewport no matter what was asked for, so the first version of these tests could never observe a clipped column and **passed against the unfixed code** — a green test that asserted nothing. Resizing the **table widget itself** is honoured. The tests now drive `_table.resize(...)` to a known viewport and compare `QHeaderView.length()` against it. (This is the same class of trap as ADR-113's tofu fonts: the offscreen platform is not a faithful stand-in for a real one.)

Full suite 309/309. No schema change.
