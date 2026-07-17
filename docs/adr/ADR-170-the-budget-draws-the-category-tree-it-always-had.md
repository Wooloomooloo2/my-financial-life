# ADR-170 — The budget draws the category tree it always had

**Date:** 2026-07-17
**Status:** Implemented
**Related:** ADR-058 (the budget matrix, the monthly view, and the Budget/Actual/Diff line shape this reshapes). ADR-024 (nearest-budgeted-ancestor bucketing — the rule that turns out to have modelled this all along). ADR-168 (per-file collapse persistence — the pattern this follows, and the one decision it deliberately inverts). ADR-092 (the per-file `setting` table). ADR-124 (why a rollover annotation lives in a tooltip, not inline). ADR-094 (bills). ADR-171 (the monthly row redesign this hierarchy makes possible).

## Context

Owner-reported, from two screenshots of a real budget:

> Lower group and leaf nodes should be slightly indented, for example 'Bills → Cable and Internet'. You should be able to collapse indented, same with being able to collapse expenses. The top node should include the sum of the group/leaf nodes in the budgeted / actual / difference.

The annual matrix drew every budgeted line as a **flat peer**, disambiguated only by a parenthetical: `Bills`, then `Cable and Internet (Bills)`, `Council Tax (Bills)`, `Digital Subscriptions (Bills)` — a tree rendered as a list, with the parent sitting alongside its own children as though it were their sibling.

The screenshot showed what that costs. `Bills` had a plan of **£982/month** and an Actual of **0.00 in every single month**, while each of its children showed a budget of **0.00** against real spending — every child red, the parent green, and a bottom line that looked like a rout.

Nothing in it was arithmetically wrong. The cause is ADR-024's bucketing rule, which `budget_calc` still applies: each in-perimeter txn lands on the **nearest budgeted ancestor** of its category. The moment a `Cable and Internet` line existed, it claimed Cable's transactions — and `Bills` stopped receiving them. `Bills` kept the plan that those transactions justified and lost the spending, so its diff ran clean and green; and because it is a rollover line (ADR-058 D3), the unspent £982 **compounded**: £1,151 → £2,643 → £3,625 → a phantom **£7,553 "available"** by December. The user had asked for a breakdown and been silently charged the parent's actuals for it.

**The decisive observation is that the residual line the owner was reaching for already exists.** Asked whether some design might let a user budget a few leaves and still "make up the difference between those and the total of the parent node — maybe an 'everything else' line appears if there's a difference?", the answer is that the `Bills` row *is* that line, and always has been: bucketing guarantees a parent row holds **exactly** the spending no budgeted child claimed. This is also why section subtotals have been correct all along — parent + descendants double-counts nothing, because each txn is in exactly one bucket.

So the model was right and the presentation was lying about it. The parent's residual figure was labelled `Bills`, which reads as *the whole of Bills*, and placed as a peer of its children, which reads as *another kind of Bills*. This is a rendering change, not a calculation change.

## Decision

**A budgeted category with budgeted descendants renders as a group: a roll-up header, indented children, and the parent's own line honestly labelled 'Everything else'.**

```
▾ Bills                    867.00        ← roll-up: own residual + all descendants
    Cable and Internet      33.00
    Council Tax            352.00
    Everything else        482.00        ← the parent's own line, renamed
```

Five decisions carry the weight.

**1. The group header is a roll-up, and therefore not editable.** Its Budget/Actual/Diff are the sum of the parent's own residual and every budgeted descendant. This is what the owner asked for, and it buys the property that makes collapsing worth having: **a collapsed group still tells the whole truth about itself**, so hiding detail costs no information. It is not editable because it is a sum with no stored allocation behind it — offering an edit there would promise a write that cannot land. `MatrixRow.is_editable` is the single predicate both views ask.

**2. 'Everything else' is the parent's real line, renamed — and hidden when it is empty.** It carries the same `line_id` as its header (a group and its residual are two views of one budget line: the whole and the remainder), so editing it writes the parent's allocation exactly as before. It is emitted **only** when it holds a non-zero allocation or actual — itemise a group to the penny and the row silently disappears; leave a gap and it reappears to account for it. That conditional is the entire answer to "I want to avoid it getting too cluttered": the row exists precisely when it has something to say.

**3. A line with no budgeted descendants is untouched.** It stays a plain, editable, un-nested leaf — the exact pre-ADR-170 shape. The group machinery costs nothing until you ask for detail, and the migration story is that there isn't one: budget `Bills` alone and it is a simple line; itemise a child and `Bills` becomes a group whose £982 becomes `Everything else: £982`, which you then drag down into the children. No data moves and the roll-up is right at every intermediate stage.

**4. Indentation replaces the parenthetical — but only where it can.** Nested under `Bills`, a row reads `Cable and Internet`, because its position already says `(Bills)`. But a line whose parent is **not budgeted** has no group to sit under and lands at depth 0, where the suffix is the only thing separating `Insurance (Car)` from `Insurance (Home)` — so there it stays. `mfl_public.mfl` is exactly this case (21 lines, every parent unbudgeted) and renders identically to before.

**5. The collapse set is shared by both views and persisted per budget, per file.** Following ADR-168: the keys embed *this file's* category ids, so app-level `QSettings` would collapse unrelated groups in other files; state lives in the file's `setting` table (ADR-092) under `budget/collapsed`, keyed by budget id within the map — two budgets over the same categories are two different views and collapse independently. The annual matrix and the monthly view render one tree and share one set, so a group collapsed on one is collapsed on the other.

**This inverts ADR-168's second decision, and the inversion is the point.** That ADR rejected a bare collapsed-key *set* in favour of a `{key: expanded}` map, because the sidebar's Closed-accounts group defaults to **collapsed** and a set cannot encode "expanded against the default". Here every group and section defaults to expanded, so there is no default for an explicit bool to protect and the set is simply the honest shape. The reasoning transfers; the conclusion does not.

### Falling out of the tree

- **Section subtotals count *top-level* rows now.** A section holding one group with four children has five rows but only one top-level row — and the group header already rolls all five up, so a subtotal beneath it would restate the identical figure. That is exactly the duplication ADR-058's "skip a single-row subtotal" rule existed to prevent; it just needed to learn about depth.
- **A group's drill covers its subtree.** Double-clicking a group's Actual must open the transactions the *roll-up* summed, or the list contradicts the number that was clicked. New `is_ancestor_or_self` containment test, and a `group` drill mode alongside `line` / `unbudgeted` / `section`.
- **The burn-down's scope test became a subtree test.** The scope combo now offers group headers, and `compute_burndown` matched `bucket == target` — which would have charted only the residual's spending against the *whole group's* available, a line that could never reach the floor. `is_ancestor_or_self` is **exactly equivalent to the old test for a leaf** (a leaf has no budgeted descendants, so nothing can bucket below it), so only groups — unscopeable before — see any change. The combo also skips residual rows, since a group and its 'Everything else' share a `category_id` and would otherwise appear twice.
- **The ↻ glyph moved to the rows that own a policy.** A group header is a roll-up over lines that each carry their own rollover, so a glyph there would claim a policy the header does not have. Its context menu, for the same reason, offers only "remove this group's own line" — there is no single rollover or role to toggle.

### The current cell, and a correction

The report came with a rider: *"when you edit anything on the annual view, the screen repaints and the user loses their position on the screen."*

The obvious reading — a model reset drops the viewport to the top — is **wrong, and was implemented before it was checked.** Driving the real window offscreen against an 83-row budget showed a `QTableView` **holds its scrollbar value** across `beginResetModel`/`endResetModel`, both when the row count is unchanged and when it shrinks. The first fix restored a saved scroll value; it "passed" only a test that was measuring its own side effects, and the elaborate machinery it grew (a row anchor, to survive a scrollbar maximum that genuinely does oscillate across post-reset layout passes) was defending against a bug that was not there.

What a reset *does* destroy is the **current index** (`currentIndex().row()` → `-1`), and `_render` runs after every edit and every `WindowActivate`. So committing a budget amount dropped the cursor: the highlight vanished from the cell just typed into, and arrowing or tabbing on to the next month restarted from nowhere. That is losing your place, and it is what `_restore_current_cell` fixes — restoring the index also brings its cell back into view, which is what makes it read as "the screen kept my place". It is re-applied on the next event-loop turn (the same `singleShot(0)` idiom the activate refresh already uses) to outlive the queued layout pass, and clamped against `rowCount()` because a refresh can shorten the table — including, now, by zeroing a residual away.

## Rejected

- **A `QTreeView` instead of a flat model with a `depth` field.** The natural-looking move, and a rewrite: the matrix is a *table* of twelve month columns whose rows are Budget/Actual/Diff triples per line. A tree model would have to express "three metric rows per node" as either three children per node (destroying the drill-down and edit paths, which key off the row) or a custom span layout — for indentation and a chevron that a pre-order list with a `depth` gives for free. The pre-order list also makes collapse a one-pass filter (`visible_rows`) rather than tree-state bookkeeping.
- **Making the group header editable, spreading a typed total down into its children.** Tempting — it is what the roll-up *looks* like it should do — and unanswerable in practice: spread it pro-rata? evenly? into the residual only? Every answer silently rewrites lines the user did not touch, and the honest place to type a group-level number already exists and is called 'Everything else'.
- **Always showing 'Everything else', including at zero.** Consistent, and precisely the clutter the owner asked to avoid. A row that says 0.00 / 0.00 / 0.00 on a fully-itemised group is noise on every group anyone has finished itemising.
- **Putting 'Everything else' first, above the children.** It is where the parent's own money sits, so it has a claim to the top. But it reads as *the remainder* — what is left after the itemised lines — and a remainder goes last. (This ADR's own first sketch put it first; writing the label out settled it.)
- **Auto-migrating the screenshot's budget** — moving `Bills`' £982 into the children, or zeroing it. Destroying data to fix a rendering bug. The £982 is a real, deliberate allocation; it now renders as `Everything else: £982`, which is what it has always meant, and the user can redistribute it or not.
- **Suppressing the parent's rollover when it becomes a group.** It would have prevented the phantom £7,553 — and it would also silently change a policy the user set. The compounding is now *visible* (the roll-up shows the plan against the real subtree spend) and the ↻ is on the residual row that actually carries it, which is the fix. Overriding a stored policy because the display used to be confusing is the wrong lever.
- **Restoring a saved scroll position.** See above: it fixes nothing, because Qt already preserves it. Kept out rather than left in as harmless-looking insurance — code that defends against a bug that does not exist is a claim about the system that the next reader will believe.

## Consequences

- **The screenshot's budget now reads as what it is.** `Bills` shows its true plan against its true subtree spending; the compounding rollover is visible rather than hiding behind a parent with no actuals; and the children's zero budgets are legible as *unallocated*, with `Everything else` naming the £482 that was never itemised.
- **Collapsing is lossless**, which is what makes it worth persisting. A collapsed group is a complete summary of itself, not a hidden one.
- **Existing budgets are unaffected unless a parent *and* a child are both budgeted.** Verified against `mfl_public.mfl` (21 lines, all parents unbudgeted → 21 unchanged depth-0 leaves keeping their suffixes) and `mfl_dev.mfl` (8 lines, no parent/child overlap → unchanged).
- A group and its residual **share a `line_id`** by design. Any future code keying a map on `line_id` across a section's rows must expect the collision; `row_kind` distinguishes them.
- Deleting a budget leaves an orphaned entry in the `budget/collapsed` map. Inert — no window will read it — and not pruned, matching ADR-168's treatment of the same drift.
- **ADR-171 depends on this.** The monthly view's row (`0.00 / 7,553.13 (+6,571.13)` and a diff column restating the carry) is the next thing to fix, and it needed the tree first.

`tests/test_budget_hierarchy.py` 7/7, Qt-free: a lone parent stays a leaf; itemising a child produces header + indented children + residual in that order, with the roll-up and the residual's own bucket (a direct txn plus an *unbudgeted* child) both exact; the residual vanishes when fully itemised; **subtotals do not double-count the headers**; indentation replaces the parenthetical except for an orphan at depth 0; grandchildren nest and roll up transitively; and the `is_ancestor_or_self` containment test.

That transitive-roll-up test earned its place immediately — it caught a **double-count in the first implementation**, where a three-level chain summed `[own] + arranged_subtree` and the arranged subtree contained both `Cable`'s header *and* the children that header had already rolled up. The roll-up now sums `own_subtree` (one row per real line), which is the same discipline the section subtotal needs and the same trap one level down.

`tests/test_budget_group_collapse.py` 6/6, driving the real `BudgetWindow` offscreen: the roll-up is not editable while its children, its residual and a childless leaf all are; collapsing hides exactly the subtree and keeps the header's total; the collapse survives a reopen and is isolated per budget; a corrupt setting degrades to fully expanded; the current cell survives a refresh (**fails without the fix** — verified by neutering `_restore_current_cell`); and a shrinking refresh does not restore off the end.

Full suite 373 passed, 1 failed — the same pre-existing, unrelated `test_drilldown_account_subset::test_split_row_double_click_opens_split_dialog` that ADR-168 recorded, confirmed failing on the untouched tree. No schema change — `budget/collapsed` is a new key in the existing `setting` table.
