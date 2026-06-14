# ADR-070 — Category lifecycle: archive / restore

**Date:** 2026-06-14
**Status:** Accepted
**Implements:** the ADR-011 reserved `archived_at` soft-delete column (category half).
**Related:** ADR-069 (the account half of the same lifecycle arc — same model, mirrored). ADR-013 / ADR-014 (category tree, kind, reparent/merge). ADR-051 (split lines reference categories). The import path walker `find_or_create_category_path`.

---

## Context

Arc D, the sibling of ADR-069. A long-lived file accumulates categories that fall out of use — an old budgeting scheme, an import that auto-created a path the owner no longer wants, a category superseded by a reparent. Today the only removal is `delete_category`, which re-points the category's transactions to **Uncategorised** and drops the row. That's destructive: the historical attribution ("this was Groceries") is lost. The owner wants to *hide* a category without rewriting history, and bring it back later.

As with accounts, the schema already reserved it: `category.archived_at TEXT` (ADR-011), and the tree/picker/budget queries (`list_category_tree`, `list_categories_flat`, `category_parent_map`, `category_kind_map`, …) already filter `WHERE archived_at IS NULL`. Nothing set it. This ADR adds the verb + the dialog UX, mirroring the account decisions: **one Archive concept** (not archive-vs-hide), **excluded by default with a "Show archived" toggle**, and **flow-report history retained** (reports aggregate `txn`/`txn_category_line` by `category_id`, never gated on `archived_at`, so an archived category's past transactions still roll up under its name).

The tree shape adds one wrinkle accounts don't have: categories are **hierarchical**, so archiving must keep the tree consistent — no active child stranded under an archived parent (it would be unreachable when archived rows are hidden).

---

## Decision

### Cascade rules (the invariant)
- **Archive cascades down the subtree.** `archive_category(id)` archives the category **and all descendants**, so a whole branch hides in one action and no active child is ever left under an archived parent.
- **Restore cascades down *and* up.** `unarchive_category(id)` clears `archived_at` on the category, its **whole subtree**, *and* every **ancestor** up to the root — so a restored node is always reachable in the default tree, even if it sat inside a previously-archived branch.
- Together these maintain the invariant **"every non-archived category has a non-archived parent (or is a root)."** The parent pickers (New / Reparent) also refuse to offer an archived node as a parent, and an import resurrects (un-archives) any matched path row, so the invariant holds across every mutation.

### Repository (`db/repository.py`)
- `CategoryNode` gains `archived: bool`. `list_category_tree(include_archived=False)` defaults to open-only (unchanged); `True` includes archived rows and sets the flag.
- New **`archive_category(id)`** (rejects the reserved Uncategorised id=1; cascades to descendants; idempotent; returns rows-changed) and **`unarchive_category(id)`** (cascades to descendants + ancestors; returns rows-changed). New private `_category_ancestors(id)` (the `WITH RECURSIVE` mirror of `category_descendants`). `delete_category` stays as the destructive variant.
- **`find_or_create_category_path`** now resurrects: when an import re-uses a path whose row was archived, it clears that row's `archived_at` rather than landing transactions on an invisible category (the `UNIQUE(parent_id, name)` constraint would block a fresh sibling anyway). Walking root→leaf, this restores the whole matched path.

### Categories dialog (`ui/categories_dialog.py`)
- A **"Show archived"** checkbox reloads the tree with `include_archived`; archived rows render greyed (slate-400) with a " (archived)" name suffix.
- One **Archive / Restore** button that flips by selection: **Restore** when every selected row is already archived, else **Archive** (the not-yet-archived, non-reserved ones). Archive confirms (noting the subtree cascade when children exist, and that history is kept); Restore is immediate.
- Parent pickers (`_build_parent_choices`) skip archived nodes, preserving the invariant.

**No migration** — `category.archived_at` already exists (ADR-011) and the reads already honour it.

---

## Options considered

- **Archive-only, excluded-with-toggle, history-retained (chosen)** — mirrors ADR-069 exactly so the two halves of the lifecycle arc behave identically. (Owner pick, carried from the account questions.)
- **Cascade archive down + restore up (chosen)** vs. "reject archiving a category that has children" (the `delete_category` rule) vs. archive-single-node-only. Reject-with-children is tedious for a branch; single-node archiving breaks the reachability invariant (active child under archived parent). Cascade keeps the tree consistent and matches how `change_kind`/`reparent` already cascade.
- **Resurrect archived rows on import (chosen)** vs. leaving them archived (txns land on an invisible category) vs. creating a new sibling (blocked by `UNIQUE(parent_id, name)`). Resurrection is the only option that both honours the constraint and keeps imported transactions visible. An import re-using a path *means* it's in use again.
- **Soft-archive via `archived_at` (chosen)** vs. a `category.status` column. The reserved column + existing read filters made it free.
- **Keep `delete_category` alongside Archive (chosen).** Archive is the everyday verb; Delete remains for genuinely unwanted categories (still re-points to Uncategorised). The dialog now leads with Archive.

---

## Consequences

### Positive
- Categories can be retired without rewriting history — archived categories' past transactions still aggregate in every flow report under their original name.
- The tree invariant means the default (archived-hidden) view never shows an orphaned child or a dangling branch.
- Imports that touch a previously-archived path "just work" — the path comes back rather than silently swallowing transactions into a hidden node.
- No migration; default query behaviour unchanged, so the budget, pickers, and reports are untouched until they opt in.

### Negative / trade-offs
- Cascade restore can un-archive children the user had individually archived earlier inside a since-archived branch — an acceptable edge case for the "restore a branch" common path.
- Reports' category checklists (Sankey, Spending) drop archived categories from their *filters*; the history still appears in unfiltered totals. A per-report "include archived" toggle is additive if needed.
- `category_descendants` / `category_has_children` / `_sibling_exists` deliberately still see archived rows (for cycle/cascade/collision correctness), so a name collision with an *archived* sibling still blocks a rename/create — surfaced as the normal collision error.

### Ongoing responsibilities
- New category reads that should respect archiving must filter `archived_at IS NULL` (or take `include_archived`).
- Keep the parent-picker archived-skip + the import resurrect in place — they're what hold the reachability invariant.
