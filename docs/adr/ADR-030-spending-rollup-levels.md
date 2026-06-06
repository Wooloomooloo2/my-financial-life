# ADR-030 — Spending Over Time rollup levels: Top / Group / Leaf

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-018 (Reports framework + first chart — established the second-tier "Group" rule); ADR-013 (Category management policy — defines the tree); ADR-014 (Category kind — defines spending semantics)

---

## Context

ADR-018 shipped Spending Over Time with one fixed roll-up rule: each expense category is bucketed to its **second-tier ancestor** ("Group" — `Expense → Groceries → Tesco` rolls to `Groceries`). The owner picked that rule against two rejected alternatives (top-level-only, leaf-level) on the assumption that the natural budget-line dimension was the right default.

After living with the report against real data, the owner flagged that the Group rule is the wrong **default** — but still the right tool for a drilldown. Two specific observations:

1. For an "at a glance, where did the money go this year" question, the top-level breakdown (`Expense` / `Auto` / `Groceries` as separate roots — whichever roots the user actually has) is more useful than the second-tier breakdown. The Group view's ~12–30 segments per bar overflow the legend; the top-level view's 3–6 segments don't.
2. Group is still the right view when answering "where in *Auto* did the money go" type questions. Removing it would force the user to filter the checklist down to one root and re-open the report whenever they wanted a different drilldown.
3. Leaf-level was the third option ADR-018 rejected. It's never going to be the *default* but for the smallest user-created sub-trees it's the right answer (e.g. a Holidays root with three leaves: Flights, Hotels, Activities).

So the right shape is a Rollup selector with three positions, defaulting to the level the owner reaches for first.

ADR-018's `_group_for` walk lives in `mfl_desktop/reports.py::category_group_map(nodes)`. The Top mode needs a symmetric helper; the Leaf mode is the identity map and needs no helper.

## Options considered

### Replace default rather than add a control

- **Pros**: less UI; the chart "just looks right" on open.
- **Cons**: the drilldown use case is real and forcing a re-open + re-filter for every drilldown is worse than a one-control click. ADR-018 already established that the report is the place to *flex* the rollup, so flipping the default isn't enough.
- Rejected.

### Tree-collapse widget instead of a combo

The deferred "hierarchical category pickers" thread (Reports round 2 backlog) is going to give the **category checklist** a tree-shaped popup with expand/collapse. The temptation is to fold rollup into that — let the user collapse the tree to whatever depth they want, no separate combo.

- **Pros**: one widget covers both selection and rollup.
- **Cons**: rollup is a single global level applied to every series; the checklist tree's expand state is per-row. Conflating them either forces every row to share an expand state (functionally the same as a combo, but harder to set) or lets per-row state differ from the rollup level (which is confusing — segments would be inconsistently sized across the legend). The picker work is its own ADR with its own trade-offs (flat-with-breadcrumbs vs tree popup, multi-select vs single-select); coupling it to rollup makes both decisions harder.
- Rejected. The combo is independent of the picker change and can ship now without prejudicing the picker design.

### Persist per-window rollup preference

- **Pros**: a user who always wants Group can stop re-selecting it.
- **Cons**: the report window is reopened from a menu, not docked. Persistence implies a settings table or a json sidecar; ADR-018 deliberately kept the report stateless. The Rollup combo is one click after open; if real use shows it's friction, persist later.
- Out of scope.

### Names for the three positions

- *Top / Mid / Leaf* — terse but "Mid" is wrong (Group is the second-tier, which is the third-from-bottom in deep trees).
- *Root / Group / Leaf* — accurate but "Root" reads as the **single** root of the whole tree, which is what we *don't* mean.
- **Top level / Group / Leaf** (chosen) — "Top level" mirrors the existing UI vocabulary in the budget setup dialog and the category dialog. "Group" is the existing word from ADR-018 and matches Banktivity's term. "Leaf" is unambiguous.

## Decision

Add a **Rollup** combo to `SpendingReportWindow` between Granularity and the From/To dates:

| Position | Map | Helper |
|---|---|---|
| **Top level** (default) | walk to the topmost ancestor (`parent_id is None`) | `mfl_desktop.reports.category_root_map(nodes)` (new) |
| **Group** | walk to the deepest ancestor whose parent is a root — i.e. the second tier | `category_group_map(nodes)` (existing, unchanged) |
| **Leaf** | identity (`category.id → category.id`) | none; computed inline |

The Categories checklist is rebuilt whenever the rollup changes:

- It lists the **distinct bucket ids** for `kind='expense'` categories under the active map.
- Uncategorised stays excluded from the checklist — its own "Include Uncategorised" toggle survives at every level (Uncategorised is itself a top-level root, so its rollup bucket is itself in all three modes).
- All items default checked on rebuild. Per-rollup preservation of which items the user had unchecked is intentionally not attempted — the bucket id set changes across rollups, and trying to map "I unchecked Groceries at Group level" onto "Top-level Expense is checked" is more confusing than starting from all-checked.

`category_root_map` mirrors the shape of `category_group_map` so the report window doesn't case-split on rollup at filter time — it just picks the active map and uses it the same way as today. Leaf mode uses an identity dict computed once at startup alongside the other two maps.

Default flips from `Group` → `Top level`. ADR-018's other defaults (Monthly, last 12 months, all checked, Include Uncategorised checked) are unchanged.

## Consequences

### Positive
- The report opens at the level the owner reaches for first; one combo click reaches the other two.
- `category_root_map` becomes the reusable helper for the **Reports round 2** rollup work — net worth report, future income / cashflow report, budget rollup if it ever surfaces.
- The change is additive: ADR-018's Group rule is preserved verbatim, just no longer the default.

### Negative / trade-offs
- The Categories checklist is now variable-length (Leaf mode against a real Banktivity-imported dataset can mean 100+ items). The widget keeps its existing 220 px height cap and scrolls; long checklists are an existing-pattern problem and don't need a v1 fix.
- Unchecking a bucket at one rollup level doesn't carry to the next. Documented above as intentional; an "Apply checklist across rollups" mode is something to add only if real use surfaces a workflow that needs it.
- Leaf mode in the chart legend will look noisy on broad date ranges. That's the same trade-off ADR-018 used to reject Leaf as the default; surfacing it as an opt-in is the right place to leave that trade-off with the user.

### Ongoing responsibilities
- Any future change to "what counts as a root" in the category tree (e.g. category folders, were they ever added) needs to update **both** `category_root_map` and `category_group_map` symmetrically.
- The hierarchical category picker (Reports round 2) is still the right shape for the checklist itself — letting the user expand a Top-level row to see/select its descendants. When that lands it slots in **next to** the Rollup combo, not in place of it: rollup controls the chart's bucket level; the picker controls which buckets are visible.
- A future `Save Chart As Image…` verb (ADR-018 noted this as deferred) is unaffected by the rollup choice; it draws whatever's on screen.
