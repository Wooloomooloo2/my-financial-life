# ADR-152 — Category & Payee "Where it goes" sunburst is a fixed top→group view, and clickable

**Date:** 2026-07-11
**Status:** Implemented
**Related:** ADR-134 (category roll-up levels: top / group / leaf). ADR-067 (two-ring donut / sunburst). ADR-083 (donut slice → drill). ADR-113 (flat single-ring donut for leaf lists). ADR-147 (drill account scope). ADR-055 (display-currency conversion in the matrix).

## Context

The Category & Payee report's right rail pins a two-ring "Where it goes" sunburst. Owner feedback: it "isn't quite right." Two problems.

1. **Wrong pair of levels.** The sunburst rendered **group → leaf** (inner ring = budget-line group, outer = individual leaf categories), while the report's main bars/table show the **top-level** category (the default "Roll up: Top level"). So the sunburst's inner ring sat one level *below* the bars and read as a mismatch — the same-looking overview showing different buckets. The owner wants it to show **top and group** — the level pair the two-ring widget "is designed really well to show."

2. **Not interactive.** The sunburst was display-only. The owner wants to click a petal and see the transactions behind it, like the rest of the report drills.

The sunburst was already built from the *full* cached matrix (independent of the primary dimension and drill), which is the right instinct — it's a stable overview, not a slave to the current view.

## Decision

**Fix the sunburst at top→group, always, and make every slice open its transactions.**

- **Top → group.** `_render_donut` now buckets each matrix cell by its **top-level** category (`category_root_map`, the same map behind the report's "Top level" roll-up) for the inner ring, and by its **budget-line group** (`category_group_map`) for the outer petals. It stays top→group **regardless** of the report's Group-by, Roll-up, Top-N or drill — a deliberately stable two-level "where the money goes" overview. At "Top level" roll-up its inner ring now lines up with the bars; at "Group"/"Leaf" it keeps the two-level overview the widget is good at rather than collapsing to match.

- **Clickable, both rings.** `DonutSegment` gains a `segment_id`, and the inner-ring hit now carries it (it was hard-coded `None`), so an inner slice is clickable like the outer children already were — the existing `account_clicked` signal now fires for any slice that carries an id. Both rings set their category id (`segment_id` = top, child `account_id` = group), and the report opens `TransactionsListWindow` via `TxnListFilter.for_category` — category filter (descendants included), **all payees**, honouring the report's current period and account scope. Net Worth's sunburst is unaffected: its inner "account type" segments set no `segment_id`, so they stay non-clickable exactly as before.

Rejected:

- **Make the sunburst track the roll-up combo** (top→group at Top, group→leaf at Group, …). The owner explicitly wants it *fixed* — a dependable two-level reference that doesn't shift under the main controls. Tracking would reintroduce the "it keeps changing" confusion.
- **Inner ring = top, outer = leaf.** Keeps the leaf granularity, but the owner said "top and group", and a top→leaf ring can explode to dozens of thin outer petals (the old group→leaf already did). Group is the readable middle level.
- **A new `slice_clicked` signal.** Reusing `account_clicked` (an int drill id) needs no new signal and no change to Net Worth; the id is interpreted in each window's own context (an account id there, a category id here). A rename would touch Net Worth for no behavioural gain.

## Consequences

- The sunburst now reads as the report's headline overview: inner ring = the same top-level categories as the bars, outer ring = one level of breakdown, and it no longer drifts when the roll-up / dimension / drill change.
- Clicking any slice — a top wedge or a group petal — opens its transactions across all payees, scoped to the report's period and accounts. The inner ring is now cursor-and-tooltip interactive too (it was inert).
- `DonutChart` stays backward-compatible: `segment_id` defaults `None`, so every existing caller (Net Worth, Income & Expense, the flat leaf donuts) is unchanged; only a segment that opts in becomes clickable.
- Verified headless against the live file: the sunburst builds 16 top segments each with its groups nested, and clicking the largest top (id 12 "Household") and its first group (id 14 "Home and Garden") opens transaction windows filtered to those categories with no payee constraint and the report's date span. Full suite green.
