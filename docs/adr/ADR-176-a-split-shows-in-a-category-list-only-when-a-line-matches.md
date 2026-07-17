# ADR-176 — A split shows in a category list only when a line matches

**Date:** 2026-07-17
**Status:** Implemented
**Related:** ADR-051 (split transactions; the split-aware category match this corrects). ADR-169 (the kind drill's split handling, fixed here too). ADR-147 (the drill proxy). ADR-014 (Uncategorised is category id 1).

## Context

Owner-reported, with a screenshot: the **Uncategorised** transaction list was full of `—Split—` rows whose split lines were all categorised. In the screenshot an ATM withdrawal split into Cash / Eating Out / Household Services — three real categories, nothing uncategorised — sat in the Uncategorised list all the same.

The cause is in the data model, correctly described by the register model's own comment:

> A split transaction has no single category — its lines carry the categories.

A split's *parent* `txn.category_id` is a placeholder. The grid renders it "—Split—" regardless of its value, and in practice it is Uncategorised (id 1). The real categorisation lives in `txn_split` rows.

Both filter proxies matched a category with:

```python
row.category_id == filter_id  or  filter_id in row.split_category_ids
```

The first clause is the leak. ADR-051 added the second clause so that filtering by a category living *only* on a split line still surfaces the "—Split—" row — a real fix. But it left the first clause in place, and for a split that clause matches the **parent placeholder**. So filtering by Uncategorised matched every split whose placeholder was Uncategorised — i.e. every split — whether or not a line was genuinely uncategorised.

This was masked for years because it is only wrong for the *Uncategorised* filter. For any other category the placeholder (Uncategorised) never equals the filter, so only the split-line clause could fire, and it fired correctly. Uncategorised is the one filter that collides with the placeholder — which is exactly the list the owner was looking at.

## Decision

**For a split, a category filter consults the lines alone; the parent placeholder never counts.**

The match becomes, in both proxies:

```python
if row.split_count:
    keep = filter_ids ∩ row.split_category_ids   # lines only
else:
    keep = row.category_id in filter_ids          # the whole-txn category
```

Since `txn_split.category_id` is `NOT NULL` and an uncategorised line stores Uncategorised (id 1, ADR-014), *"a split line that is actually uncategorised"* is precisely `1 ∈ split_category_ids`. So the Uncategorised list now shows a split only when a line is genuinely uncategorised — the owner's ask, stated exactly.

This preserves everything ADR-051 and ADR-169 intended:

- Filter by a line's own category → the split still surfaces (the split-line clause is unchanged).
- Filter by Uncategorised → the split surfaces only when a line is uncategorised (the fix).
- A non-split row is unaffected — it has no lines, so it matches on its own `category_id` exactly as before.

Applied in **three** places, all the same one-line-of-logic error:

- `TransactionFilterProxy` (base) — `set_category_id`, kept capable for non-UI callers though the register no longer drives it (its category combo went in 2026-06-14).
- `DrillDownFilterProxy._category_descendant_ids` — what the Uncategorised list actually uses (`_apply_filter` → `set_category_descendant_ids`). This is the surface in the screenshot.
- `DrillDownFilterProxy` kind path (`_kind_cat_ids`, ADR-169) — the Income & Expense report drill. The same placeholder leak: a report scope that happens to include Uncategorised would surface a split none of whose lines are in scope. ADR-169's own test already asserts the split match is "a real intersection, not 'any split'", so this aligns with its intent rather than changing it — and its tests pass unchanged.

## Rejected

- **Fix only the drill proxy** (the reported surface) and leave the base proxy and kind path. Same bug, three copies; fixing one and leaving two identical latent leaks is how a bug comes back under a different report. The base proxy is currently unused by the register UI, but it is public API kept "capable for other callers", so it is fixed and tested rather than left as a trap.
- **Give a split parent a NULL category_id** so it never matches any category filter. A schema and data-migration change to fix a display-filter bug, and it would break every place that reads the parent `category_id` as a non-null (the FK, the "—Split—" render, imports). The placeholder is fine; the filters just had to stop trusting it.
- **Exclude all splits from the Uncategorised list.** Wrong in the other direction: a split with one genuinely uncategorised line *should* appear there, because that line needs categorising. The owner said as much — "unless there is a split line that is actually uncategorised".
- **Touching the report aggregations.** Checked and unnecessary: `spending_aggregates`, the income/expense series, and the uncategorised *count* all read the split-unrolled `txn_category_line` view (ADR-051), which already attributes each split line to its own category. The bug lived only in the two `TransactionRow`-based filter proxies, which see the parent placeholder. Recorded so the next reader knows the aggregation side was verified, not overlooked.

## Consequences

- **The Uncategorised list shows only genuinely-uncategorised work again** — plain uncategorised transactions and splits with an uncategorised line. The fully-categorised `—Split—` rows are gone. Reproduced against the screenshot's exact shape before and after.
- **No behaviour change for any other category filter** — the leak was Uncategorised-specific (the only filter that collides with the placeholder). Every other drill was already correct via the split-line clause and is untouched.
- The three proxies now share one rule stated three times; a future fourth category filter should follow it. There is no shared helper (the proxies are independent Qt classes with different field names), so the rule lives in aligned comments rather than one function — noted as a small duplication, not worth a base-class hook for three call sites.

`tests/test_drilldown_split_uncategorised.py` 6/6: a fully-categorised split is **absent** from Uncategorised (the bug), a split with an uncategorised line is **present**, a plain uncategorised row is present, filtering by a split line's own category **still surfaces** the split (ADR-051 preserved), and the base proxy agrees with the drill proxy. The base-proxy guard was confirmed to **fail against the old parent-matching logic**. ADR-051, ADR-147 and ADR-169's drill tests all still pass (17/17 across the two drill files).

Full suite 423 passed, 0 failed. No schema change.
