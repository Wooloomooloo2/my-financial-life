# ADR-013 — Category management policy: rename / reparent / merge / delete with explicit rejections over silent cascades

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design) — defines `category` as `parent_id` self-referencing adjacency with `UNIQUE(parent_id, name)`, the reserved `Uncategorised` row, and `txn.category_id NOT NULL`; ADR-012 (Payee name-management policy) — same rename / merge / delete shape, extended here with reparent

---

## Context

The desktop app's category-management UI exposes five operations on the hierarchical category tree: **New**, **Rename**, **Reparent**, **Merge**, **Delete**. The first three are unsurprising; Merge and Delete touch user data in ways that need explicit policy decisions.

The category tree differs from the flat payee table in three ways that drive this ADR:

1. **Hierarchy.** A category has a parent and may have children. Reparenting is a real verb; cycle prevention is a real constraint; sibling-name uniqueness is per-parent, not global (`UNIQUE(parent_id, name)` per ADR-010).
2. **Required reference.** `txn.category_id` is `NOT NULL` without a cascade rule — every transaction always has a category. Deleting a category can't leave transactions in an undefined state; they must be reassigned. ADR-010 made `Uncategorised` (id=1) the reserved sink for exactly this purpose.
3. **Cascade-collision morass on subtree merges.** Merging a category that has subcategories means deciding where the children go. If they move with the merged-from node, every child becomes a potential sibling-name collision under the target. If they don't move, the FK's `ON DELETE SET NULL` rule on `parent_id` would silently demote them to top-level — a quiet, hard-to-undo restructure.

The owner shares the packaged app with non-technical users. The pattern from ADR-012 — *destructive operations are explicit verbs with confirmation dialogs that name the affected rows; rejections beat silent cascades* — is the starting position.

## Options considered for each verb

### Rename collision — reject vs auto-merge
Same question as ADR-012. **Chosen: reject** with an error directing the user to Merge. Renaming "Food" to "Groceries" where both already exist as siblings should not silently reassign hundreds of transactions on what looks like a single-row edit.

### Reparent collision — reject vs auto-merge into the existing sibling
Moving `Travel` under `Expense` when `Expense → Travel` already exists is the same shape: two clicks producing a destructive merge. **Chosen: reject** with the same Merge-instead message.

### Reparent cycle — schema-level vs application-level prevention
SQLite does not enforce acyclic constraints on self-referencing FKs. Without prevention, a user could move `Expense` under `Expense → Subscriptions`, creating a cycle that breaks every recursive descendant query. **Chosen: application-level reject** using a `WITH RECURSIVE` descendant query before the UPDATE.

### Merge with subcategories — allow-with-cascade vs reject
The "allow" path means: when merging A into B, A's children become B's children (subject to sibling-name collisions on each child), and A is deleted. Children's children also move. Collisions at any level abort the whole operation. Implementable, but the failure modes are hard to explain in a confirmation dialog ("this would also try to move X under Y but Y already has an X, so the whole merge is rejected — go fix that first") and the success mode silently restructures a tree the user may have spent time arranging. **Chosen: reject any merge whose sources still have subcategories**, with a message that names the offending category and tells the user to reparent or merge those children first. One-level merges are simple, predictable, and reversible by the user.

### Delete with transactions — block vs reassign-to-Uncategorised
ADR-010 made `Uncategorised` the reserved deletion sink precisely so that deleting a category never strands transactions and never blocks on FK violations. **Chosen: reassign to Uncategorised** after a confirmation that names the count. The user retains the option to recategorise them later from the register.

### Delete with subcategories — cascade-children vs reject
SQLite's `ON DELETE SET NULL` rule on `parent_id` (per ADR-010) would silently promote a deleted category's children to top-level — a structural change the user didn't ask for. **Chosen: reject** and tell the user to reparent or delete the subcategories first. Same shape as the merge rule above: keep the destructive operation confined to one level at a time.

### Delete Uncategorised — allowed vs reject
ADR-010 fixed `Uncategorised` (id=1) as the reserved deletion sink; without it, the cascade-children and reassign-on-delete rules above have nowhere to point. **Chosen: reject** at the Repository layer, with a clear message at the UI layer.

### Merge target — selected categories only vs allow brand-new top-level
ADR-012 added typed-new-name targets to the payee Merge for the canonicalisation case (`Tescos` / `Tesco's` / `TESCO` → fresh `Tesco`). The same case applies to categories: a user may want to merge several import-created categories into a brand-new canonical top-level category that doesn't exist yet. **Chosen: allow** a typed brand-new name as the merge target; create it as `source = 'user'` at top-level. A typed name that matches a top-level category **outside** the current selection is rejected, mirroring the ADR-012 rule. Subcategory-level brand-new targets are not offered in v1 — they add complexity (parent picker inside the merge picker) for a case the user can also handle by creating the target first and re-selecting.

## Decision

The category-management UI follows these rules, enforced at the Repository layer and surfaced with clear messages at the dialog layer:

| Verb | Allowed | Rejected | Reassignment |
|---|---|---|---|
| **New** | Pick any parent (or top-level), supply a name | Empty name; sibling-name collision under the chosen parent | n/a |
| **Rename** | Single-row only | Empty name; sibling-name collision (message directs to Merge) | n/a |
| **Reparent** | Single-row only; target is `None` or any node not in the moved subtree | Target == self; target is a descendant (cycle); sibling-name collision at target (message directs to rename or merge first) | n/a |
| **Merge** | 2+ sources; target is one of the selected or a brand-new top-level name | Any source has subcategories; typed target matches an existing top-level category outside the selection | All transactions on sources → target, atomic; sources deleted |
| **Delete** | 1+ leaf categories that aren't `Uncategorised` | `Uncategorised` (id=1); any selected category has subcategories | All transactions on the deleted categories → `Uncategorised` |

## Consequences

### Positive
- Every destructive verb is explicit. No verb silently restructures the tree; no verb silently reassigns transactions without a named confirmation.
- The category tree's invariants — sibling-name uniqueness, acyclicity, every transaction has a category, Uncategorised always exists — are surfaced to the user as actionable messages, not as opaque SQL errors.
- Merge and Delete stay at one level at a time. A user who really wants a deep restructure performs a sequence of small, reversible steps rather than a single operation whose effects are hard to predict and harder to undo.
- The brand-new-top-level target on Merge gives the canonicalisation flow (`Tescos` / `Tesco's` / `TESCO` → fresh `Tesco`) without inviting the user to silently absorb categories from elsewhere in the tree.

### Negative / trade-offs
- A deep restructure (move a whole subtree under a new parent, then merge two subtrees) requires several clicks instead of one. This is deliberate; the savings of a one-click cascade do not outweigh the cost of a silently broken tree.
- Subcategory-level brand-new merge targets aren't offered in v1. The workaround is to create the target with the desired parent first, then include it in the merge selection.

### Ongoing responsibilities
- The Repository remains the single point of enforcement for the rejection rules above. The dialog reuses the same predicates (`category_has_children`, `category_descendants`, `_sibling_exists`) only for pre-flight messaging; the actual ENFORCE-or-error must happen inside the Repository methods so any future CLI / scripting path inherits the same guarantees.
- If a brand-new merge target is created during the picker and the user then cancels the confirmation, the new category row must be deleted. Otherwise a cancelled merge would leave an orphan top-level category that didn't exist before the click.
- The Uncategorised id (1) is hard-coded in the seed data and in `UNCATEGORISED_ID` in both the Repository and the categories dialog. Any future schema change that renumbers it must update both call sites; better: a sentinel query `SELECT id FROM category WHERE name = 'Uncategorised' AND parent_id IS NULL` if id stability ever becomes uncertain.
