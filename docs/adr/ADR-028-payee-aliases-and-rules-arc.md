# ADR-028 — Payee aliases, canonical labels, and the auto-categorisation arc (planning)

**Date:** 2026-06-06
**Status:** Proposed — planning ADR; per-round implementation ADRs will follow when each round starts
**Related:** ADR-012 (Payee name-management policy — will be **amended** after round 1 to record the canonical/alias distinction); CLAUDE_CONTEXT backlog item "Categorisation rules engine" (now absorbed into round 3 of this arc); the same-day conversational thread on orphan payee cleanup (dissolved by aliasing — see below).

---

## Context

After the same-day round of small UX additions (right-click Create Schedule From Transaction, bulk-edit payee typeahead), the owner asked a smaller question — "when I rename a typo'd payee to its canonical form, can the orphan be auto-cleaned?" — that opened into a bigger architectural one:

> "Maybe we should separate the list of rule-payees from payees visible in the register? Ideally when a transaction is downloaded, you'd want the engine to either auto-rename to the preferred label and then apply the category rule (which might be derived from the original or the cleanup). Not everyone will clean up their payee list, it's just something I am fastidious about and I see other folks sometimes are too."

This is the architecture every mature personal-finance app converges on. Banktivity calls them "Payee Aliases", Quicken calls them "Memorized Payees", YNAB has "Payee renaming rules". The shape is:

- A **canonical payee** (the user's preferred label, e.g. "Tesco") carries metadata.
- One or more **aliases** map raw import strings ("TESC*GROCERIES 0123 LONDON") to that canonical.
- An **import-time engine** rewrites the raw payee to the canonical (or matches by pattern alone) and applies an auto-categorisation rule.

Today's MFL data model lacks all three:

- The `payee` table has `id`, `name UNIQUE`, `default_category_id`, `archived_at`. Every distinct as-imported name creates a row. No canonical/alias concept.
- The `rule` table exists in the schema (added in migration 0001) but no service uses it — listed under "Other deferred items" in CLAUDE_CONTEXT.
- Import lookup is name-based: `get_or_create_payee(name)` returns an existing match or inserts a new row. There's no rewriting pass.

Two problems compound:

1. **Typo accumulation.** Every CSV import that comes through with a slightly different format creates a new payee row. The Payees dialog gradually fills with noise; the typeahead suggests noise; reports see N versions of the same merchant.
2. **No automation at import time.** The user has to re-categorise each new payee even when the rule is obvious ("anything from Tesco is Groceries"). The schema-reserved `rule` table is the right hook, but it's never been wired up.

The orphan-cleanup question from earlier in the same conversation (auto-delete payees that drop to zero references after a rename) is a symptom-level fix — it cleans the worst leftovers but doesn't address the root cause. The proper fix is *don't create the orphan in the first place* — make it an alias of the canonical, or rewrite the raw payee at import time.

The owner's two import paths fall out naturally:

- **Path A — fastidious user, clean register.** Raw "TESC..." matches an alias → txn stores `payee_id = canonical_id` → category derived from canonical's `default_category_id`. Register shows "Tesco".
- **Path B — no cleanup, register shows raw text.** Raw "TESC..." matches a `rule` by pattern (matching the raw text or memo) → txn stores `payee_id` = a brand-new payee with the raw name, no canonical link, but the category is auto-applied. Register shows the raw text "TESC*GROCERIES 0123 LONDON" but it's already categorised as Groceries.

Both paths use the same underlying primitives — aliases and rules — wired differently. The data model needs to support both without forcing either.

## Options considered (schema for canonical / alias)

This planning ADR records the schema decision now so round 1 can lean on it. The three options were discussed in conversation; recapped for the durable record:

### A. Self-referential `canonical_id` on the `payee` table (chosen)

Add one nullable column to the existing `payee` table:

```sql
ALTER TABLE payee ADD COLUMN canonical_id INTEGER
    REFERENCES payee(id) ON DELETE SET NULL;

CREATE INDEX idx_payee_canonical ON payee(canonical_id);
```

Semantics:
- `canonical_id IS NULL` → "I'm a canonical payee."
- `canonical_id IS NOT NULL` → "I'm an alias; my canonical is row `canonical_id`."
- Aliases can't carry their own `default_category_id` (round 1 enforcement: `CHECK (canonical_id IS NULL OR default_category_id IS NULL)` or app-level guard). Metadata lives on the canonical.
- Two-level only: an alias's `canonical_id` must point at a row whose own `canonical_id IS NULL`. Trees-of-aliases prevented at the Repository layer.

Queries:
- Typeahead (Payee inline editor, bulk-edit dialog, etc.): `WHERE canonical_id IS NULL` — aliases are hidden by default, only canonicals are suggested.
- Display lookup in reports / register: `COALESCE(canonical_id, id)` rolls aliases up to their canonical so "5 Tesco transactions" stays "5 Tesco transactions" not "3 Tesco + 2 TESC*GROCERIES".
- Payees-management dialog: shows canonicals at the top level with an expandable "aliases for this payee" list per row (UI tbd in round 1).

One table, one migration, one new column, no join surface change for the common paths. Existing `txn.payee_id` keeps its current meaning — it stores whichever id was assigned at insert time, and the canonical lookup happens on read.

### B. Separate `payee_canonical` + `payee_alias` tables (rejected)

Conceptually cleanest — aliases and canonicals are different entities, modelled separately:

```sql
CREATE TABLE payee_canonical (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    default_category_id INTEGER REFERENCES category(id) ON DELETE SET NULL,
    archived_at TEXT
);
CREATE TABLE payee_alias (
    id INTEGER PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES payee_canonical(id) ON DELETE CASCADE,
    name TEXT UNIQUE NOT NULL
);
```

Rejected because:
- `txn.payee_id` would need to become `(txn.payee_kind, txn.payee_id)` (which table?) or split into two columns. Either change ripples through every reads/joins. The cost is high.
- Every typeahead / register / report query gains an extra UNION or two-step lookup.
- The cleaner conceptual model isn't worth the SQL surface explosion at MFL's scale (single-user, ~thousands of rows).

### C. Lean on the `rule` table for aliasing (rejected)

The `rule` table already has `set_payee_id` — a rule that fires could rename a payee. Use rules-only for aliasing too:

```sql
-- rule.match_pattern → set_payee_id (canonical) + set_category_id (auto-cat)
```

No new schema. Reuses an already-reserved hook.

Rejected because:
- Conflates two verbs that should be independent. **Alias** = "this raw name *is* that canonical name" (a renaming statement). **Rule** = "transactions matching this pattern get this category and/or this payee" (an automation statement). Rules can fire on aliases; aliases shouldn't *be* rules.
- Path B above (no-cleanup, category-only) needs a rules engine WITHOUT touching the payee. Rule-based aliasing makes that confusing — would the rule run "category set, payee not set" be a separate kind of rule from "payee set, category set"?
- The owner's stated mental model treats aliases as a separate concept (he explicitly named them: "the list of rule-payees from payees visible in the register"). The data model should match the mental model.

## Decision

- Adopt **Option A** (self-referential `canonical_id` on the `payee` table) as the schema direction for the arc.
- Implementation proceeds in **three rounds** in this order:

### Round 1 — data model + manual alias management (one sitting, polish-round scope)

Owner-managed aliasing. The import engine doesn't change; the user creates aliases by hand from the Payees dialog. Closes the "I want to clean up my payee list" use case without any import-engine work.

**In scope:**
- Migration 0007: add `payee.canonical_id` column + index, app-level invariant for the two-level rule.
- Repository methods: `set_alias_of(alias_id, canonical_id)`, `promote_to_canonical(payee_id)`, `list_canonical_payees()`, `list_aliases_of(canonical_id)`. Plus auto-rollup of `canonical_id` on a single payee delete (aliases of a deleted canonical detach to NULL — they become canonical themselves).
- Payees-management dialog gains a "Make alias of…" verb on selected rows. Aliases are visually distinguished and grouped under their canonical.
- Typeahead delegates (register inline edit, bulk edit) filter aliases out — only canonicals are suggested.
- Reports / spending aggregates roll aliases up to their canonical for display.
- `update_transaction_payee` and `bulk_update_transactions` continue to work the same way (no auto-cleanup). If the user wants the "rename + reassign every txn pointing at the typo" flow, the existing merge verb in the Payees dialog does that already (ADR-012); this round adds the gentler "leave the typo as a permanent alias" option.

**Out of scope (deferred):**
- Import-time matching — happens in round 2.
- Pattern / fuzzy alias matching — round 2.
- Any UI for *bulk* alias management (e.g. "match all payees starting with 'TESC' to Tesco").

**Acceptance:** owner can clean up an existing typo'd payee list by hand. ADR-012 gets amended to record the canonical/alias model and the "merge vs alias" distinction.

### Round 2 — import-time alias lookup (its own arc, multiple sittings)

Wire the import engine to look up aliases on the way in. Naked starting scope is exact-match alias lookup; pattern/fuzzy is its own follow-up inside the arc.

**Likely shape:**
- New step in `parse_and_stage` / `_classify_and_stage`: for each parsed row, look up the raw payee text against `payee.name` (canonical or alias). On hit, the staged row's `payee_id` resolves to the canonical (via `COALESCE(canonical_id, id)`).
- On no-hit: today's `get_or_create_payee` behaviour stays (new payee row, canonical by default). User can later make it an alias from the Payees dialog.
- Pattern match (LIKE / regex): a second-pass that runs only when the exact match misses. Patterns live on the `payee` row itself? Or in a new column? Open — round 2 will decide.
- Fuzzy match (edit distance, token sets): explicit deferral inside the round 2 arc. Cheap first version: token-set match on uppercase-normalised names; expensive version: a proper trigram or Levenshtein index. Defer until exact + pattern coverage is shown to be insufficient.
- Open question: what does the preview step show when a raw payee matched an alias? Probably the canonical name with the raw in a tooltip — owner-confirmable at commit time. Decided in round 2.

**Out of scope:**
- Category derivation — that's round 3.

### Round 3 — rules engine (its own arc)

Wire up the `rule` table. This is the long-deferred backlog item finally getting addressed.

**Likely shape:**
- `rule` table gains matcher columns (`match_payee_pattern`, `match_memo_pattern`, `match_amount_min`, `match_amount_max`, `match_account_id`) — design tbd in round 3.
- On import (after alias rewriting from round 2): every staged row runs through the rules engine; matching rules set `category_id` (and optionally `payee_id` for Path B's no-cleanup-but-categorise behaviour). Conflicts resolved by `rule.priority`.
- Rules-management UI: a Manage → Rules… dialog that mirrors Manage → Schedules / Categories / Payees. Per-rule edit dialog with the matcher fields + setter fields.
- Open question: do rules run only on import, or also on manual entry? Open question: do rules apply retroactively to existing txns? Both decided in round 3.

**Out of scope:**
- Machine-learning categorisation (no, ever — keeps everything inspectable).

## Consequences

**ADR-012 will be amended after round 1.** The current "rename rejects on collision, merge is the reassign verb" rule stays. New rules added: "make-alias is the third reassign verb (leaves the alias's history alone, just routes future typeahead/reports through the canonical)"; "aliases are hidden from typeahead by default"; "deleting a canonical promotes its aliases to canonical (orphan-prevention)".

**CLAUDE_CONTEXT backlog absorbed.** The "Categorisation rules engine" line in CLAUDE_CONTEXT's "Other deferred items" is now round 3 of this arc — gets removed from that list when the arc starts. The orphan-cleanup question from earlier in the same conversation is dissolved: typos become aliases (cheap, recoverable), not deletes (lossy). The "naked-payees-only cleanup" idea is *not* shipping — Path A makes it unnecessary, Path B doesn't have orphans by construction.

**Reports' aliasing rollup.** Every report / aggregate / typeahead query in the codebase needs a tiny update during round 1 — wherever payee identity matters, replace `txn.payee_id` with `COALESCE(p.canonical_id, p.id)`. Most call sites are already going through Repository helpers that can do it in one place. The Spending Over Time chart's grouping (per payee, if that's ever an axis) and the Net Worth report (which doesn't group by payee) both unaffected.

**Migration is additive.** Adding `canonical_id` doesn't break existing data — every existing payee row is canonical by default (`canonical_id IS NULL`). No backfill, no destructive change.

**Scheduled_txn unaffected.** `scheduled_txn.payee_id` works the same way as `txn.payee_id`. If a scheduled txn was created against an alias and the alias is later promoted to canonical / re-assigned, the schedule's `payee_id` continues to point at the same row — no migration needed there either. The display rollup (`COALESCE(canonical_id, id)`) applies in the Schedules dialog the same way it does in the register.

**Two-level rule is a Repository invariant.** SQLite doesn't have a CHECK constraint expressive enough to prevent `payee → canonical → canonical` chains in pure DDL. The Repository's `set_alias_of` rejects any attempt to alias against a non-canonical target. Aliases of aliases aren't possible by construction; aliases-of-the-thing-I-just-promoted-to-canonical is, but only after the canonical is canonical.

**Owner trust in recommendation.** Owner explicitly accepted the recommendation ("Option A, sketch the planning ADR now, I'll go with your recommendation") rather than picking the schema himself. Per the standing rule, the ADR records the rationale anyway — even when delegated, the alternatives and reasoning are written down. If a future round surfaces a constraint that breaks Option A, this ADR can be superseded; nothing about it is irreversible.

**Not solved by this planning ADR (deliberately).**
- Per-round implementation details (round 1 / 2 / 3 each get their own ADR when they start).
- The exact UX for "make this payee an alias of…" in the Payees dialog (round 1 decides).
- Match strategies in round 2 — exact only? regex? fuzzy?
- Rule priority semantics + retroactive application in round 3.
- Whether to expose a "Show aliases" toggle in the Payees dialog so the user can audit / unalias (round 1 decides).
- How aliasing interacts with the import-time potential-match merge logic from CLAUDE_CONTEXT (probably round 2's problem).

**Scheduling.** Owner asked me to recommend ordering. Recommended: slot round 1 *before* the Reports round 2 work from the same-day backlog, since the typeahead and reports rollup helpers from round 1 make the hierarchical category picker (Reports round 2's biggest item) cleaner to build on a payee model that's stable. Rounds 2 and 3 can come after Reports round 2 — they're bigger work without a UI dependency on Reports.
