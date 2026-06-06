# ADR-029 — Payee aliases round 1: data model + manual alias UI

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-028 (planning ADR for the three-round arc — locked the schema choice and round split); ADR-012 (Payee name-management policy — **amended** by this round); ADR-010 (transactional schema — the `payee` table this round extends).

---

## Context

Per ADR-028's plan, round 1 of the payee-aliases arc is a polish-scoped sitting that:

- Adds the data model (the `canonical_id` self-reference on the `payee` table).
- Exposes the alias verbs in the Payees-management dialog.
- Filters aliases from the payee typeahead.
- Leaves the import engine and rules engine untouched (rounds 2 and 3).

The goal of round 1 is **the owner can clean up an existing typo'd payee list by hand and have the typeahead stop suggesting the typos** — without touching imports, without a rules engine, without changing how transactions are stored at import time. Round 2 will wire the import engine to look up aliases; round 3 will build the rules engine on top.

## Options considered (round 1 — implementation-level)

The schema choice (self-referential `canonical_id`) was settled in ADR-028. The decisions below were settled during round 1 implementation.

### Where the rollup math runs — SQL or Python (chosen: Python)

The Payees dialog needs to display, per canonical, a *rolled-up* usage count that includes every alias's direct txn count. Options:

- *Single SQL query with two LEFT JOINs and a CASE*: keep all the math in the database. Possible — `LEFT JOIN` to a subquery that sums per-canonical alias counts; for canonical rows display `direct + summed_alias_total`; for alias rows display `direct`. Worked through; the SQL is readable but moderately heavy and inflates the dialog's only query.
- **Two-pass Python over a flat list** (chosen): `SELECT id, name, canonical_id, c.name AS canonical_name, direct_count FROM payee ...` returns one flat list. The repository builds rolled counts in a dict and emits PayeeRow with the rolled count for canonicals and direct count for aliases. The payee table is small (typical tens to low hundreds); the rollup is one linear pass over the rows.

Two-pass Python keeps the SQL one line, lets `PayeeRow` carry both `usage_count` (rolled, what the dialog displays) and `direct_usage_count` (for callers that need to distinguish), and means the same query feeds every Payee-management surface today and in future. Negligible runtime cost.

### Display: table vs tree, sort behaviour (chosen: table, sort-off)

- *QTreeWidget* with canonicals as top-level rows and aliases as children: the right conceptual model, but a meaningful refactor on top of the existing QTableWidget Payees dialog. Bigger scope than round 1 wants.
- **Keep QTableWidget; render aliases indented with "↳ " prefix; disable column-sort; rely on the Repository's grouped order** (chosen): the Repository returns rows in `(canonical → its aliases)` order; the dialog preserves that verbatim. Column 0 carries the indent + prefix; column 1 ("Alias of") spells out the relationship in plain text so the grouping survives copy-paste and screen-reading. Sorting the Name column would interleave canonicals and aliases alphabetically across the whole list and lose the grouping — disabled deliberately. Filter box still works (matches against the Repository's stored name, not the indented display text).

Column 2 ("Used in") shows the rolled count for canonicals (own direct + every alias's direct) and the direct count for aliases. Owner reads "Tesco · 142" as "all the Tesco-ish transactions" — the rolled total is what's useful, not the canonical's bare direct count.

### Buttons and gating (chosen: two new buttons, contextual enable)

Two new verbs, with the existing four kept as-is:

- **Make Alias of…** — enabled whenever **at least one** row is selected. The picker (editable combo over existing canonicals) decides the target. A canonical selected alongside aliases can still be the target (others in the selection become aliases of it). A canonical that's not the picked target becomes an alias too — the verb is symmetric over the selection.
- **Promote to Canonical** — enabled when **at least one** row is selected **and all selected rows are currently aliases**. A mixed selection (some aliases, some already canonical) disables the button. Owner wanted promote to be a deliberate verb, not a no-op-on-canonicals fall-through.

Alternative considered and rejected: a single "Alias…" verb that contextually does the right thing (promote if all aliases, alias-to-target if not). Too clever — two named verbs map cleanly to two intents.

### What to do with txns that point at the new aliases (chosen: leave them in place)

When `set_alias_of` makes payee A an alias of payee B, every txn currently pointing at A still has `txn.payee_id = A.id`. Options:

- *Re-point txns onto B*: makes the alias rows pure metadata; A becomes a dead row referenced by nothing. Essentially the same as merge. Lose the alias's standalone identity in history.
- **Leave txns in place** (chosen): txns continue to point at A; display routes A→B via `canonical_id`. The alias row carries its own historical identity (it's stored, you can promote it back, you can see it in the Payees dialog). The user's merge verb is the alternative when they want the re-point behaviour; making alias-of *not* merge keeps the two verbs distinct.

Owner's wording in the conversation drove this: "Make this typo a permanent alias" vs. "Merge these typos away forever." Two intents, two verbs.

### Whether the register's per-row display should roll up via canonical (chosen: not in round 1)

The Repository's `list_transactions_for_account` and `list_all_transactions` queries currently select `COALESCE(p.name, '') AS payee_name`. Adding a second LEFT JOIN on `payee.canonical_id` and `COALESCE(cp.name, p.name, '')` would make the register show "Tesco" for txns pointing at `TESC*GROCERIES` aliased to Tesco — closer to the owner's "I want my register to show the preferred label" intent.

Rejected for round 1 because of a subtle pitfall in the inline payee typeahead's edit path: the editor opens with `index.data(Qt.EditRole)` as its initial value. If the displayed name is the canonical (via rollup) but the underlying `payee_id` is the alias, the user pressing Enter without changing the text writes "Tesco" back through `get_or_create_payee` → re-points the txn from the alias row onto the canonical. Silent rewrite-on-no-change. Surprising. Not worth fixing in round 1 since round 2's import-engine work is where canonicalisation naturally belongs (the txn's `payee_id` gets set to the canonical at import time, not at display time).

Round 1 keeps the register display verbatim. Aliased txns continue to render their stored payee name (the alias's name) until either: (a) the user re-edits them inline, picking the canonical from the now-filtered typeahead, or (b) round 2 ships and future imports route the raw text to the canonical at insert time. The relationship is visible in the Payees dialog. Owner can flag if this conservative choice doesn't match the actual day-to-day experience after living with it.

### Merge: how to handle aliases of merge sources (chosen: re-point onto target before deleting sources)

If a merge source had aliases of its own, the FK rule on the new `canonical_id` column says `ON DELETE SET NULL` — those aliases would auto-promote to canonical when the source is deleted. **Wrong** for a merge: the user clearly wants everything routed under the target, not detached as freshly-canonical strays.

`Repository.merge_payees` now re-points aliases of sources onto the target *before* the sources are deleted, inside the same SQL transaction. Three statements: re-point aliases, re-point txns, delete sources. Boring obvious correction; recorded here so a future reader of the code sees the *why*.

### Delete: what happens to aliases of a deleted canonical (chosen: auto-promote, FK does it)

The new `canonical_id` column's FK is `ON DELETE SET NULL`. Deleting a canonical that has aliases auto-promotes the aliases to canonical (they reappear in the typeahead). This is the behaviour the planning ADR called for ("orphan prevention by construction"). The delete confirmation dialog mentions this so the user isn't surprised — "Aliases of this payee (if any) will become canonical."

### Filter box matches what the user typed in the Repository, not the indented display (chosen: pass)

The display name is `"    ↳ " + p.name` for aliases. A user typing "tesco" in the filter wouldn't match the indented display "↳ Tesco" if the filter ran against rendered text. The filter helper reads the stored name from a `(payee_id → PayeeRow)` lookup table built at load time, so the match is always against the Repository name. Trivial detail but the kind of thing that's painful to debug if you don't get it right.

## Decision

### Schema

Migration `0007_payee_canonical.sql`:

```sql
ALTER TABLE payee ADD COLUMN canonical_id INTEGER
    REFERENCES payee(id) ON DELETE SET NULL;
CREATE INDEX idx_payee_canonical ON payee(canonical_id)
    WHERE canonical_id IS NOT NULL;
```

Existing rows are canonical by default (`canonical_id IS NULL`). Partial index — aliases only, since canonicals dominate and `WHERE canonical_id IS NOT NULL` is the only access pattern that uses the index.

### Repository

- `PayeeRow` extended with `canonical_id: Optional[int]`, `canonical_name: Optional[str]`, `direct_usage_count: int`. Existing `usage_count` becomes the rolled count for canonicals (own direct + aliases' direct) and the direct count for aliases. Default values on the new fields preserve back-compat for any caller still constructing PayeeRow positionally.
- `list_payee_names()` updated: `WHERE archived_at IS NULL AND canonical_id IS NULL`. Aliases removed from the typeahead source.
- `list_payees_with_usage()` rewritten: one SQL query returning flat rows with `(id, name, canonical_id, canonical_name, direct_cnt)`; Python rolls alias counts up to their canonical and emits `PayeeRow`s in canonical→aliases order.
- `list_canonical_payees() -> list[(int, str)]` — sorted canonical-only list for the "Make Alias of…" target picker.
- `list_aliases_of(canonical_id) -> list[(int, str)]` — round-1 helper, will be useful in round 2 / 3.
- `set_alias_of(alias_id, canonical_id)` — validates two-level rule (target must be canonical, source must have no aliases of its own, source ≠ target) and applies. ValueError on rule violation; atomic commit on success.
- `promote_to_canonical(payee_id)` — sets `canonical_id = NULL`. Idempotent for already-canonical rows.
- `merge_payees(sources, target)` updated to re-point aliases of sources onto the target before deleting sources. One additional UPDATE inside the existing transaction.

### Dialog

`payees_dialog.py`:

- 3 columns (Name / Alias of / Used in) instead of 2. Aliases indented with `↳ ` prefix and italic font; "alias of *canonical_name*" in column 1 in dark grey.
- Sorting disabled — the Repository's grouped order is the display order.
- Filter matches against the stored name, not the rendered display text.
- Two new buttons: **Make &Alias of…** (enabled when ≥1 selected) and **&Promote to Canonical** (enabled when ≥1 selected and all are aliases).
- Alias-target picker mirrors the merge-target picker shape: editable combo seeded with canonicals; typing a brand-new name creates a new canonical; rejecting "alias to self" and "alias to an existing alias" with clear errors.
- Per-source `set_alias_of` errors collected and surfaced in one summary so a two-level-rule violation on one source doesn't block the rest.

### Display surfaces (deliberately deferred)

- Register inline display: still shows the stored payee name (no canonical rollup). Reason in "Whether the register's per-row display should roll up…" above. Round 2 sets `txn.payee_id` to the canonical at import time, which solves the same problem at a place where the silent-rewrite pitfall doesn't apply.
- Reports payee aggregation: no current report aggregates by payee (grep came up empty). When one is added (probably as part of the Reports arc), it'll need the `COALESCE(canonical_id, id)` rollup — recorded here so the future Reports ADR can lean on it.

## Consequences

**Migration is additive.** Existing data is canonical by default. No backfill, no destructive change. Down-migration is `ALTER TABLE payee DROP COLUMN canonical_id` (SQLite supports column drop since 3.35); aliases would just become "canonicals with a weird typo name" — i.e. the pre-round-1 state.

**ADR-012 amended.** A canonical/alias amendment section was added at the end of ADR-012. The Rename / Merge / Delete policies stand; the two new verbs and the "merge re-points aliases" subtlety are recorded.

**Typeahead becomes immediately useful for cleanup.** Once an alias is set, it disappears from the typeahead. The register's inline editor and the bulk-edit completer both refresh on next open (both call `list_payee_names()` per editor open, so no cache invalidation needed).

**Per-row register display still shows aliases verbatim until the user re-edits.** Acceptable per the round-1 conservatism. Round 2's import-engine rewriting and (if owner wants it after living with it) a per-row rollup are the cleaner paths.

**Two-level invariant is the Repository's responsibility.** SQLite CHECK isn't expressive enough; `set_alias_of` rejects target=alias and source-has-aliases. A future migration could try a trigger-based enforcement, but Repository-level is good enough for a single-user app.

**Merge / Delete consistently handle aliases.** Merge re-points aliases onto the target (deliberate). Delete promotes aliases of the deleted canonical to canonical (FK rule). Both behaviours surface in the confirmation dialogs.

**Round 2 won't need a schema change for exact-match alias lookup.** `get_or_create_payee` could be extended in round 2 to look up the canonical via `COALESCE(canonical_id, id)` when it finds an existing alias by name. Or — more likely — round 2 will introduce a new `find_canonical_for_raw(raw_text)` method that handles exact and pattern match. Either way, the round-1 schema is enough to support it.

**Round 3's rules engine sits on top of this, not next to it.** The `rule` table from migration 0001 stays untouched in round 1. Round 3's rule matchers can fire on either canonical (e.g. "anything pointing at canonical Tesco gets category Groceries") or raw text (e.g. "anything matching `TESC*` gets aliased to Tesco AND categorised as Groceries"). The round-1 schema is forward-compatible.

**Reversible.** The whole round is one migration + ~150 lines of Repository + the dialog file. Reverting goes back to the pre-round-1 state with the down-migration above; the existing rename/merge/delete behaviour was preserved.

**Not solved by round 1 (deliberately, per ADR-028):**

- Import-time alias lookup (round 2).
- Pattern / fuzzy alias matching (round 2).
- Rules engine (round 3).
- Bulk alias management (e.g. "match all payees starting with TESC to Tesco" via a dialog wizard). Owner can do this by selecting matching rows and clicking "Make Alias of…" — round 1's UI is enough for the cleanup use case.
- Register inline display rollup (deferred to round 2 or beyond).
- Reports payee aggregation (no current call site — when added, will need rollup).
