# ADR-072 — Payee→category memory (Arc G round 1)

**Date:** 2026-06-15
**Status:** Accepted
**Implements:** ADR-028 round 2 (import-time alias lookup) + the per-payee category half of the arc's "memorised payee→category" goal.
**Related:** ADR-029 (round 1 — `payee.canonical_id` aliases + manual management). ADR-012 (payee name-management). ADR-013/014 (categories, kind). ADR-051 (split transactions — their category lives in `txn_split`). ADR-071 (investment rows carry no payee). The owner's round-3 sketch (a unified Payees & Rules screen) and the pattern-rules engine are **Arc G round 2** (their own ADR).

---

## Context

Arc G is the auto-categorisation arc. After every import the owner re-categorises the same merchants by hand — "anything from Tesco is Groceries" — even though the app could remember it. The schema has long reserved the hooks (`payee.default_category_id` since migration 0001; the `rule` table since 0001), but **nothing reads or writes either**, and the import engine does **zero** payee/category lookup — it just calls `get_or_create_payee(raw)` and stores whatever category the source file carried (usually none → Uncategorised).

ADR-028 planned this as a three-round arc. Round 1 (aliases) shipped (ADR-029) but deliberately left two gaps for round 2: imports don't resolve a raw string to its canonical, and there's no category automation. This ADR closes the highest-value slice — **a payee remembers its category, and imports apply it** — and defers the pattern-rules engine + unified management screen to round 2.

The owner picked the shape (`AskUserQuestion`): **import-memory first** (this round); memories created by a **confirm prompt** (not silent learning, not manual-only); retroactive application **offered each time** (not always, not never), and only ever onto **Uncategorised** rows.

---

## Decision

### The memory lives on `payee.default_category_id` (the canonical)

A payee's remembered category is stored on the **canonical** payee row's existing `default_category_id` column — so every alias of the same merchant ("TESCO", "TESC*GROCERIES 0123") shares one memory. No migration: the column has existed since 0001. Reads and writes resolve an alias to its canonical first (`_canonical_id_for`). `category_id = Uncategorised (1)` is treated as "no memory" and clears the column.

This is deliberately **separate from the `rule` table** (ADR-028 Option C, rejected there): an alias/default-category is an *identity/preference* statement that belongs to a payee; a rule is a *pattern automation*. Round 2 adds the rule engine alongside this, not instead of it.

### Import resolves raw → canonical and applies the memory

New `Repository.resolve_import_payee(raw_name) -> (payee_id, default_category_id)`:

- Empty name → `(None, None)` (investment rows carry no payee, ADR-071).
- Exact name match: if the matched row is an **alias**, the returned id is its **canonical** — so new ledger rows point at the canonical (ADR-028 round 2: the register shows the clean name immediately, and the alias's history rolls up without a read-time `COALESCE`). Existing alias-pointing rows are untouched; this just stops creating new ones.
- No match: a new canonical payee is created (today's behaviour).
- The second element is the canonical's `default_category_id`.

In `ImportService.commit_import`, the **plain-insert** branch now fills the category from the memory **only when the row would otherwise be Uncategorised** and isn't a split: `if category_id == uncategorised and payee_default and not tx.splits: category_id = payee_default`. A category the source file *did* carry always wins; the memory never overwrites. Split parents keep `category_id = Uncategorised` (their categories live in `txn_split`), so they're excluded. Re-import dedup is unaffected — cash hashes on `payee_raw` (the raw string), not `payee_id`, so normalising the stored id to the canonical changes no hash.

### Capturing a memory: confirm-on-categorise (register inline edit)

When the user sets a **non-transfer** category inline on a register row whose payee has **no memory yet**, a `MemoriseCategoryDialog` offers *"Always categorise **Tesco** as **Groceries**?"* with an opt-in checkbox *"Also apply to N existing uncategorised Tesco transactions"* (shown only when N > 0, checked by default). Accept → `set_payee_default_category`; checkbox → `apply_default_category_to_uncategorised`, then the model reloads.

**Only fires when no memory exists.** Categorising a single Tesco row differently when a memory is already set is a one-off override, not a re-prompt — *changing* a memory is done in the Payees dialog. This keeps the prompt from nagging.

### Managing memories: the Payees dialog

`PayeesDialog` gains an **Auto-category** column (the canonical's remembered category, full breadcrumb path) and an **Auto-&category…** button (single selection) that opens a category picker (with a **Clear** verb). Setting a real category there also offers the same retroactive apply when uncategorised rows exist. `PayeeRow` gains `default_category_id`; `list_payees_with_usage` selects it.

### Retroactive apply is Uncategorised-only

`count_uncategorised_for_payee` / `apply_default_category_to_uncategorised` operate over the canonical **and all its aliases** (`expand_canonical_payee_ids`), touching only rows with `category_id = Uncategorised`, `transfer_id IS NULL`, and **not** a split parent (`id NOT IN (SELECT txn_id FROM txn_split)`). A category the user already set is never overwritten.

---

## Consequences

- **The core "memorise" loop works:** import → uncategorised rows → categorise Tesco once → confirm → every future Tesco import is auto-categorised, and (optionally) the back-history too.
- **No migration, no new dependency.** Uses two columns that have existed since 0001.
- **Aliases now normalise at import** (ADR-028 round 2): new rows point at the canonical, so the alias-pointing-history that `expand_canonical_payee_ids` was built to bridge stops growing.
- **`payee.default_category_id` is now load-bearing** — `delete_payees`/merge already move/clear payee rows via FK; the column rides along (an alias is set on the canonical, which survives a merge as the target).

### Deferred to round 2 (their own ADR)

- The **pattern-rules engine** (`rule` table): `contains` / `starts-with` / `ends-with` / `is-exactly` matchers on `payee_raw`/`memo`, setting payee and/or category, with priority — the owner's "if contains TFL then Transport" case, and the **unified Payees & Rules management screen**.
- **Manual-entry pre-fill** (New/Edit Transaction dialog auto-filling the category when a remembered payee is chosen) and **bulk-edit memorise** — small additions, folded into round 2 to keep this round's surface tight.
- **Surfacing an "N auto-categorised" count** in the import summary (would touch `ImportResult` + every caller; not worth it yet).

### Rejected alternatives

- **Silent learning** (auto-set the memory from the last/most-common categorisation) — less control; the fastidious owner wants to approve what's remembered.
- **Manual-only** (memory set only in the Payees dialog) — misses the natural capture moment (categorising an imported row).
- **Store the memory on the `rule` table now** — conflates identity and automation (ADR-028 Option C); round 2 adds rules as a distinct concept.
- **Always-overwrite on retroactive apply** — would clobber deliberate one-off categorisations; Uncategorised-only is the safe contract.

---

## Verification

Offscreen, against a temp DB:

- `resolve_import_payee`: empty → `(None, None)`; unknown → new canonical, no default; alias name → returns the **canonical** id + the canonical's default category.
- `set/get_payee_default_category` resolve alias→canonical; Uncategorised clears.
- Import: a row with no source category and a remembered payee lands on the memorised category; a row whose file carried a category keeps it; a split parent stays Uncategorised; re-import dedups (hash unchanged).
- `count_/apply_default_category_to_uncategorised`: counts/updates only Uncategorised non-transfer non-split rows across canonical + aliases; returns the row count; leaves already-categorised rows alone.
- Offscreen Qt: `MemoriseCategoryDialog` returns the apply-to-existing flag; the Payees dialog renders the Auto-category column and the picker/clear flow.
