# ADR-169 — The Income & Expense drill honours the report's category scope

**Date:** 2026-07-16
**Status:** Implemented
**Related:** ADR-083 (the report drill-downs, including this kind-based one). ADR-088 amend (category narrowing on the Income & Expense report — the scope this drill was ignoring). ADR-147 (the drill-down's *account* subset scope — the same class of bug, one field over). ADR-129 (the net-of-refunds expense definition the drill reconciles with).

## Context

A user reported that on the Income & Expense report, clicking a bar drills into transactions **outside the report's scope**. Their "Bedford House ROI" report is narrowed to Landlord Expenses, and its expense bars are computed from only those categories — but clicking a bar opened a transaction list showing pet food, groceries, everything.

The report already supports a category scope (ADR-088 amend): `IncomeExpenseFilters.category_ids`, expanded to descendants and passed to `income_expense_series`, which applies `t.category_id IN (...)` in SQL. So the **chart is correct**. The drill-down is where it leaks.

The chain: `_on_segment_clicked` built a `TxnListFilter.for_kind(...)` carrying the period, the cash-flow kind, and the account scope — **but not the category scope**. The drill window then resolved the kind's category set from `list_categories_flat(kinds=(kind,))` — *every* category of that kind in the file — and filtered rows to that. A report scoped to one category drilled into all of them. This is the exact shape of the ADR-147 bug (a report scoped to a *set of accounts* drilled into every account), one field to the left: the aggregate applied the filter, the drill didn't.

Fixing the scope surfaced a **second bug behind it**, in the same drill, that the owner hit immediately: on a real "Bedford House ROI" report, clicking a *non-zero* income bar drilled to an **empty** list. The report aggregates over `txn_category_line` — a **split-aware** view — so its income for that year (£14,340) was entirely **split lines** categorised "Landlord — Rental Income" on parent transactions whose own category is Uncategorised. The kind drill matched a row's own `category_id` and never looked at its split lines, so every one of those parents failed the match: correct scope, but drilling into nothing. (Once scope was applied, this stopped being masked by the flood of out-of-scope rows.)

## Decision

**Thread the report's (descendant-expanded) category scope through the kind drill, and intersect it with the kind's categories.**

Three pieces, mirroring how ADR-147 threaded the account subset:

**1. `TxnListFilter` carries the scope.** A new `kind_category_ids: tuple[int, ...]` field, populated by `for_kind`. Empty means "the report spans every category of the kind" — the historical behaviour, so a report with no category narrowing is unchanged. `_on_segment_clicked` fills it with `_expanded_category_ids(self._current_filters.category_ids)` — the *same* expansion the chart query uses, so the drill and the bar agree on exactly which categories (parents pull in their descendants, ADR-088 amend).

**2. `_apply_filter` intersects, it doesn't replace.** The kind's full category set is still resolved from `list_categories_flat`; when a scope is present it is **intersected** with it (`kind_ids &= self._kind_category_ids`) rather than substituted. Intersection is deliberate: a report scope can legitimately hold categories of *both* kinds (it narrows income and expense together), and intersecting with the clicked kind's set drops the other kind's categories cleanly — you can't leak an income category into an expense drill. This matches `income_expense_series`, which applies the kind clause *and* the `category_id IN` clause together.

**3. An empty intersection means "match nothing", not "match everything".** `DrillDownFilterProxy.set_kind_filter` previously treated a falsy category set as "no restriction" (`frozenset(ids) if ids else None`). Once we intersect, an empty result is reachable in principle (a scope with no category of the clicked kind), and the old contract would have silently reopened the whole kind — the very leak we're closing. It now distinguishes `None` (no restriction) from an *empty set* (reject every row) via `is not None`. In practice a rendered bar implies in-scope flows exist, so the empty case is essentially unreachable through the UI — but the proxy's contract is now correct regardless of who calls it.

**4. The kind match is split-aware, mirroring the category-descendant drill.** A row now clears the category gate when its own `category_id` **or any of its split-line categories** (`row.split_category_ids`) is in the scoped kind set — the identical `isdisjoint` test the category-descendant path (ADR-051) has always used one branch up. This is what makes the Bedford-House income bar drillable: the split parents surface via their rental-income line, and double-clicking one opens the split dialog as usual. Its consequence is the **sign gate**: `amount > 0` (income) / `< 0` (expense) is applied **only to whole-transaction rows** (`split_count == 0`), where `amount` genuinely is the categorised flow. A split parent's *total* sign says nothing about the matched line — the report counts the line, not the parent — so a split that cleared the category gate is kept for the user to open. (Trade-off: a split whose only in-scope income line is a negative refund is surfaced even though the report's `amount > 0` line filter wouldn't have counted it — the same mild over-inclusion the category-descendant drill already accepts, and the parent genuinely touches the category.)

## Rejected

- **Replacing the kind's category set with the report scope instead of intersecting.** Would work when the scope holds only same-kind categories, but a mixed-kind scope (income + expense narrowed together) would then let an income category through the expense drill on sign alone. Intersection is the honest reconstruction of what the bar counted.
- **Filtering the drill in SQL, re-running `income_expense_series`-style.** The drill-down is a live `QSortFilterProxyModel` over the register model (so its rows stay editable and update in place); it filters in `filterAcceptsRow`, not by re-querying. Threading the id set into the existing proxy is consistent with how every other drill facet (account subset, category subtree, payee roll-up) already works.
- **Leaving `set_kind_filter`'s falsy-means-all contract alone.** Tempting, since the empty case is unreachable via the UI today — but a proxy method that does the opposite of its argument in an edge case is a landmine for the next caller. One `is not None` closes it.

## Consequences

- A scoped Income & Expense report drills into exactly its own categories — the drill reconciles with the bar, the way ADR-147 made the account-scoped drill reconcile with its totals.
- An unscoped report (no category narrowing) is unchanged: empty `kind_category_ids` → no intersection → every category of the kind, as before.
- The scope joins `signature()`, so two drills that differ only by category scope open as two distinct windows rather than colliding in the single-instance registry.
- The kind drill is now split-aware, closing the empty-drill-on-a-non-zero-bar bug: income (or expense) that lives only on split lines is drillable, reconciling the drill with a report bar computed over `txn_category_line`. This brings the kind drill in line with the category-descendant drill, which was already split-aware — the two sibling drills no longer disagree about splits.

`tests/test_drilldown_kind_category_scope.py` 8/8: the proxy excludes out-of-scope categories; a `None` scope still shows all of the kind; an empty intersection matches nothing (the `set_kind_filter` contract); income on a split line is surfaced (the Bedford-House shape) and so is a split's expense line under an expense scope, while a split whose lines are all out of scope is excluded; and, end-to-end, `for_kind` with a scope threads through the window to show only the scoped category, while `for_kind` without one shows all. The existing `test_drilldown_account_subset.py` (the sibling account-scope drill) still passes bar its one pre-existing, unrelated failure. No schema change.
