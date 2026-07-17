# ADR-171 — The monthly budget row says what it means

**Date:** 2026-07-17
**Status:** Implemented
**Related:** ADR-170 (the category tree — landed first, and this is the other half of the same report). ADR-058 R3 (the monthly view, and D3's rollover/carry). ADR-124 (the annual grid's inline carry annotation, moved to a tooltip for the same reason). ADR-159 / ADR-165 (`currency_symbol` as the app's one glyph definition — this closes the last surface printing an ISO code). ADR-167 (the frozen-theme-colour ratchet, which this takes to zero for this module). ADR-076 (the token layer).

## Context

The owner, on the monthly view: *"I still think it looks pretty messy. It's one of the worst report views in the app, we can do better."*

ADR-170 fixed the tree it draws. What was left is the row itself:

```
Bills ↻  [████████░░]  0.00 / 7,553.13 (+6,571.13)   +7,553.13
```

**Four numbers, and no two of them independent.** `0.00 / 7,553.13` is spent-of-available; `(+6,571.13)` is the rollover carried in; `+7,553.13` is the diff — which is just available − spent, i.e. a restatement of the two numbers to its left. The carry annotation and the diff are largely the same fact told twice, in two notations, neither labelled.

And the signed diff **means different things in different rows**. Positive is favourable (ADR-058), so `+7,553.13` on an expense means money left, while on income it means over-target. The reader has to know the row's kind before they can read its last column. For income the failure is worse than ambiguity: being part-way through a month and not yet having earned the full month's pay — the ordinary state of every month — rendered as a **red deficit**.

Then the case that gives the game away. Rollover carries an overspend *backwards* (ADR-058 D3, deliberately unclamped), so `available` goes negative and the row becomes:

```
Cable and Internet (Bills) ↻  [██████████]  32.99 / -158.63 (-158.63)  -191.62
```

`32.99 / -158.63` is not a sentence. There is no budget of −£158.63 for £32.99 to be "of"; the bar is full red because the fraction is undefined and there is nowhere for it to go; and the reader is asked to subtract two negatives to find out they are £191.62 over.

Rendering the view to PNG — rather than trusting the strings — turned up two more, neither visible to an assertion:

- **The Pool / Assigned / Unallocated line was drawn twice**, a centimetre apart, *with different numbers*. Both `BudgetWindow` and `BudgetMonthlyView` carry one; the window's is pinned to **today's** month, the view's follows its **selector**. On the Monthly page both were visible, so scrolling to March showed two lines with the same three labels disagreeing.
- **Old rows kept painting under the new ones.** `_clear_layout` called `deleteLater`, which does not unparent: a widget taken out of a layout stays a *child* of the list and keeps painting at its old geometry until the deferred delete lands. The bottom of the list rendered as overlapping text.

And two straggler defects the redesign walked into:

- **`Pool: GBP 5,000.00`.** ADR-159 declared `currency_symbol()` "the single definition of the glyph" and ADR-165 deleted sixteen private currency tables to make that true. This view survived both — because it has **no table to find**. It just printed `f"{ccy} {amount}"`: the same defect with nothing to grep for.
- **Three frozen light-theme hexes** (`_MUTED`, `_GREEN_TXT`, `_RED_TXT`) — the last three on ADR-167's ratchet for this module — so in dark mode the remainder text was light-theme green on the dark canvas.

## Decision

**Two numbers and a sentence.**

```
▾ Bills                 [███░░░░░░░]   £580.75 of £3,799.00    £3,218.25 left
    Cable and Internet  [█████████░]      £32.99 of £33.00         £0.01 left
    Digital Subs        [██████████]      £55.76 of £40.00       £15.76 over
    Everything else     [█░░░░░░░░░]   £140.00 of £3,374.00    £3,234.00 left
```

**1. `spent of available`, and the carry moves to the tooltip.** "of" is the relationship, spelled. The carry is not a third number in the headline — it is an *explanation* of the second one, and it has room to be that in a tooltip: *"Budgeted £10.00 + £6,571.13 rolled over = £6,581.13 available this month"*. This is exactly the move ADR-124 made when the same annotation was overflowing the annual grid's month column; the monthly view kept it inline and paid the same price in a different currency.

**2. The remainder is worded, not signed.** `£3,218.25 left` / `£15.76 over` for spending; `£500.00 to go` / `£668.90 above plan` for income. Saying the word removes the sign-decoding *and* the kind-dependence in one stroke — and it lets income under plan be **muted rather than red**, because a month that is not over yet is not a failure. The colour still carries the signal; it no longer carries it alone.

**3. A non-positive `available` gets different words.** `£20.00 spent` + `£50.00 over` — no "of", because there is no budget to be of. The rule is `available > 0`, not `!= 0`: a zero budget with spending against it is the same sentence as a negative one.

**4. One Pool line per screen.** The monthly view's wins on its own page — it sits beside the selector it tracks — and the window's hides there. Two lines showing the same three labels with different numbers is worse than either alone: it reads as a contradiction until you work out that one of them is pinned to a month you are not looking at.

**5. `setParent(None)` before `deleteLater`.** Removes the widget from the visible tree on the spot. `deleteLater` still does the freeing (never `del` — this can run from a signal handler on the widget being cleared). Fixed in `_clear_goals_bar` too, which had the identical pattern.

**6. Money gets its glyph; the inks come from tokens.** `_money()` delegates to `currency_symbol`, sign outside the glyph (`-£20`, never `£-20`), and an unknown currency still falls back to a spaced code — ADR-165's rule that **money is never printed without its unit**. The three hexes become `_muted_ink()` / `_good_ink()` / `_bad_ink()`, resolved at render time so they follow a live theme toggle; each token's light value equals the hex it replaced, so light mode is pixel-identical. The ratchet entry for this module drops **3 → 0**.

Fixed column widths went 190/160/78 → **248/190/126**: the old split predates both the tree indent (which eats into the name) and the currency glyph (which widens every amount), and clipped both. Names now **elide** with the full text in the tooltip, because a `QLabel` clips by default — `Digital Subscriptions` simply vanished mid-word with nothing to say it had.

## Rejected

- **Keeping the diff column as a signed number and only fixing the colour.** The smaller change, and it leaves the reader decoding a sign whose meaning depends on the row's kind. The words are shorter to read than the rule for interpreting the number.
- **Showing a percentage** (`63% spent`) instead of, or beside, the remainder. It is what the bar already says, less precisely, and it is unreadable in exactly the case that matters — a negative available has no meaningful percentage.
- **Clamping `available` at zero so the negative case disappears.** This is a *display* problem, and clamping would fix it by lying: the deficit is real, ADR-058 D3 carries overspends backwards on purpose, and hiding it would leave the row cheerfully reporting a £0.00 budget while £158 of last month's overspend quietly went unaccounted for.
- **Dropping the row's bar for a number-only list.** The bar is the one part that was working: it reads at a glance and it is the drill target. The numbers were the problem.
- **Removing the monthly view's own Pool line and keeping the window's.** Backwards. The window's is pinned to today's month, so on a page whose entire purpose is stepping between months it would show the wrong month's numbers the moment you press ◀.
- **`del w` / `sip.delete` instead of `deleteLater`.** Immediate destruction of a widget that may be mid-signal is how ADR-058's macOS teardown bugs happened. `setParent(None)` solves the painting; `deleteLater` keeps the freeing safe.

## Consequences

- The screenshot's worst row, `32.99 / -158.63 (-158.63) → -191.62`, now reads `£32.99 spent` · `£191.62 over`. The information is identical; the arithmetic is no longer homework.
- **The last `GBP 822.64` in the app is gone** — ADR-159's "one definition of the glyph" is now true of every surface, not just every surface with a table to grep for.
- **`budget_monthly_view.py` is at zero frozen colours** and the ratchet is tightened to hold it there. ADR-167's staleness check *demanded* this: it fails a module whose allowance is looser than its actual count, so cleaning the three forced the entry down. The ratchet worked exactly as designed, unprompted.
- Verified by **rendering both themes to PNG**, which is the only reason the duplicate Pool line and the ghost rows were found at all — both tests pass on the string content and fail on the pixels. ADR-167 made the same point about the reconcile wizard's invisible ink; it keeps being true.
- The tooltip is now the only place the carry is stated on this view. That is a real trade — a rolling line's arithmetic is one hover away rather than on the row — and it is the trade ADR-124 already made on the annual grid, so the two views are at least consistent about it. The ↻ glyph still marks the row from across the screen.

`tests/test_budget_monthly_row.py` 8/8, driving the real window offscreen: the row is two numbers and a worded remainder; an overspend says "over"; a **carried-in deficit says what was spent instead of offering a negative budget** to be "of"; the carry is out of the row and explained in the tooltip; **income under plan is muted, never red**; money carries its glyph (including the unknown-currency fallback and sign-outside) and the pool line has no ISO code; a rebuild leaves **no ghost rows**; and **one Pool line per screen**.

The last two guards were written against bugs found in a PNG, and both were confirmed to fail with their fixes neutered — the ghost guard reporting **11 orphaned rows** still parented to the list.

Full suite 381 passed, 1 failed — the same pre-existing, unrelated `test_drilldown_account_subset::test_split_row_double_click_opens_split_dialog` recorded by ADR-168 and ADR-170, confirmed failing on the untouched tree. No schema change.
