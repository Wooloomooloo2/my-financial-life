# ADR-014 — Category kind: per-row column over derived-from-root

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design) — defines the `category` table extended here; ADR-013 (Category management policy) — rename / reparent / merge / delete rules extended here with the kind dimension

---

## Context

Reports and cashflow need to interpret signed transaction amounts in context: a positive amount on an expense category is a refund; a negative amount on an income category is a correction (e.g. clawback); transfers between the user's own accounts are neither income nor expense and should not contribute to either total. Today the schema has nothing to distinguish these — every category is just a name under a parent.

Two questions follow:

1. **Where does the kind information live?** The schema currently has Income and Expense as seeded top-level rows (per the seed in 0001) with everything else nested underneath. The kind could be *derived* from the root ancestor of any category (Approach A — tree-segregated), or stored *explicitly* on each category row (Approach B — per-row column).
2. **What kinds exist?** Income and expense are obvious; "transfer" is the third — money moving between the user's own accounts, which should not double-count in either the income or the expense column.

The owner's mental model comes from Banktivity, which stores a "type" attribute on each category (`Income`, `Expense`, `Transfer`). Existing top-level categories in MFL may include user-created and import-created rows that aren't under Income or Expense (Banktivity import paths often produce top-level leaves like `Auto`, `Food`, etc. when no hierarchy separator was present). A pure tree-segregated approach would require migrating all of those under one of the kind roots before the model is consistent — disruptive on a populated database.

## Options considered

### Option A — Tree-segregated by kind (derived from root ancestor)

Four reserved top-level roots: `Income`, `Expense`, `Transfer`, `Uncategorised`. Every other category must live under one of them. Kind is determined by walking up to the root. Reports compute kind on the fly (or with a materialised view).

- Pros: structure equals meaning, no separate column, no possibility of kind/parent drift.
- Cons: requires migrating every existing top-level non-reserved category under a root. Removes the user's ability to organise categories in their own top-level groupings. Banktivity model is not this.

### Option B — Per-category kind column (chosen)

Each `category` row stores its own `kind` value, constrained to `('income','expense','transfer')`. Sub-categories inherit their parent's kind at creation time; reparenting across kinds is a deliberate, explicit operation; reports do a single SELECT JOIN, no recursion.

- Pros: matches Banktivity's mental model. Backfill is a one-shot recursive CTE — no structural change required. The user keeps the freedom to arrange the tree as they like; kind is orthogonal to placement. Reports become trivially indexable.
- Cons: kind and parent are independently mutable. The dialog must enforce that subcategories' kinds stay in sync with their parent's at creation, and a reparent across kinds must be a deliberate operation with cascade to descendants.

### Cross-kind reparent — silent / confirm / reject

- *Silent auto-update*: dragging a category under a different-kind parent silently changes its kind. Easy to implement; bad UX — the change isn't surfaced and reports change without warning.
- *Confirm explicitly* (chosen): show a dialog that names the old and new kind and states that the change cascades to subcategories.
- *Reject*: refuse cross-kind reparents; force the user to recreate the category under the right root and merge. Strictest; matches Option A's tree-segregated model.

### Cross-kind merge — allowed / silent-kind-change / reject

- *Allowed, kind unchanged*: source rows are deleted, transactions follow the target's id, target keeps its kind. But the merged-from transactions silently become "kind = target's kind" in reports without the user being told. Surprising.
- *Allowed with confirmation*: same as above but with a dialog explaining the change.
- *Reject* (chosen): refuse cross-kind merges; tell the user to convert kind first via reparent. Mirrors the reparent confirmation step and keeps Merge a one-dimensional operation.

### Default kind for Uncategorised — expense / special-cased

- *Expense* (chosen): Uncategorised behaves as an expense category in reports. The vast majority of uncategorised transactions in personal finance are spending, and the user can still reclassify individual transactions. Three-kind model stays clean.
- *Special "unspecified" kind*: a fourth enum value just for Uncategorised, surfaced in reports as "needs categorisation". Cleanest semantics but adds complexity for a sink the user is meant to drain anyway.

### Default kind for import-created top-level — expense / kind-from-amount

- *Expense* (chosen): every freshly-created top-level import category defaults to `kind=expense`. Subsequent imports inherit. The user reclassifies in the category dialog if needed.
- *Inferred from the importing transaction's sign*: positive → income, negative → expense. Brittle (refunds make expense categories look like income on first import), and surfacing the wrong default puts the burden on the user to detect a silent miscategorisation later.

## Decision

**Per-category `kind` column** (Option B), values `('income','expense','transfer')`, enforced by a CHECK constraint on `category.kind`. Added by migration `0002_category_kind.sql`:

1. `ALTER TABLE category ADD COLUMN kind TEXT NOT NULL DEFAULT 'expense' CHECK (...)`.
2. Backfill via recursive CTE: rows whose root ancestor is the seeded `Income` (id=2) become `kind='income'`; everything else becomes `kind='expense'` (Uncategorised's own row and every other top-level pre-existing row included).
3. Insert `Transfer` as the third seeded top-level system row, `kind='transfer'`.

**Dialog rules** (enforced at both the Repository layer and surfaced with messages at the dialog layer):

- *New top-level*: user picks the kind from a combo (default `expense`).
- *New sub-category*: kind combo locked to the parent's kind; sub-categories inherit silently.
- *Reparent intra-kind*: kind unchanged.
- *Reparent cross-kind*: explicit confirmation dialog naming both old and new kind, and stating the cascade to subcategories. Confirming sets `kind` on the moved row **and every descendant**.
- *Reparent to top-level*: kind unchanged (the moved category becomes its own root and keeps the kind it had).
- *Change Kind (direct)*: enabled at any level. The user picks the new kind from a combo; the change cascades to every descendant. When the new kind would differ from the selected sub-category's *parent* kind, a warning is shown ("your tree will show mixed kinds — you can reconcile via Reparent") so the consequence is explicit, but the operation is not blocked. The Banktivity-style mental model the owner is coming from treats kind as a free attribute on each category, so over-restricting this verb fails the user's expectation. Drift is recoverable via Reparent (move under the matching root) at any time.
- *Merge intra-kind*: target's kind applies to the merged transactions, no change in semantics.
- *Merge cross-kind*: rejected with a message directing the user to reparent first.
- *Merge target = brand-new top-level*: the new row is created with the shared kind of the source selection (which is uniform because cross-kind selections are rejected up front).
- *Delete*: unchanged — reassigns transactions to Uncategorised, which is itself `kind='expense'`.

**Import-created categories** default to `kind='expense'` at the top level. Sub-segments inherit from their (just-created or pre-existing) parent.

## Consequences

### Positive
- Reports can join `txn` to `category` and bucket by `kind` with no recursion. Cashflow becomes `SUM(amount) GROUP BY kind`.
- Refund semantics fall out for free: `(kind='expense' AND amount > 0)` → refund row.
- The category tree retains its current shape — no migration disruption, no forced restructuring of the user's existing top-level rows.
- Banktivity-style "category has a type" maps directly to the column, so the owner's mental model from the previous tool transfers intact.

### Negative / trade-offs
- `kind` and `parent_id` can drift if a reparent skips the dialog (e.g. a future scripting path). The Repository's `reparent_category(category_id, new_parent_id, new_kind=None)` enforces "kind change only when caller asks" but a careless caller could update parent without the matching kind. The remedy is consistency at the Repository layer — *all* reparents go through `reparent_category`, never raw UPDATE.
- Reports must treat `Uncategorised` carefully: although it has `kind='expense'`, a positive amount there shouldn't necessarily be reported as a "refund" — it's more likely uncategorised income. Reports should surface Uncategorised's transactions as a separate line item ("needs categorising") rather than rolling them into the refund total. (Recorded as a report-side concern, not a model change.)
- The four-kind option ("unspecified" for Uncategorised) remains available behind a future migration if reports surface a need to distinguish unknown from expense. The CHECK constraint just gains a fourth value; existing rows keep `expense`.

### Ongoing responsibilities
- The Repository's `create_category` and `reparent_category` must continue to be the single point through which kind changes happen. Any new code path that mutates `category.parent_id` (e.g. bulk import, a future categorisation rules engine) must route through `reparent_category` so the kind cascade rule is preserved.
- `find_or_create_category_path` (used by import) creates new top-level rows with `kind='expense'`. If a future import format encodes the kind on its categories (some bank exports do — Banktivity's CSV does), the parser should pass that through rather than defaulting.
- When the report layer ships (next milestone), it should validate at startup that every `category.kind` is one of the three known values — defensive against any future schema drift. The CHECK constraint already enforces this, but a startup probe documents the assumption.
