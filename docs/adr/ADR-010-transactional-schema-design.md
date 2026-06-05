# ADR-010 — Transactional schema design

**Date:** 2026-06-05
**Status:** Accepted

---

## Context

[ADR-009](ADR-009-storage-engine-for-ledger-data.md) chose SQLite as MFL's primary store. This ADR fixes the concrete schema for the rewrite: the table set, key shape, currency representation, the category model, and the constraint behaviours that follow from earlier product decisions.

The schema must support:

- The register surface validated by the v0.1 web app and the PySide6 prototype — filter, sort, search, paginate, inline edit, running balance.
- Per-lot cost basis and IRR/ROI for investment accounts.
- The import workflow (OFX/QFX/CSV/QIF) carried forward from v0.1 (`app/core/import_engine/`), including duplicate detection and the manual-entry/import match flow.
- Hierarchical, dual-sourced categories — see [`project-categories-design`](../../../.claude/projects/C--Users-hallm-Documents-GitHub-my-financial-life/memory/project_categories_design.md) in memory.
- MRL-style identifier compatibility (ADR-006) — stored as opaque text columns on rows that need to refer to MRL entities.

The reference SQL is in [`docs/schema.sql`](../schema.sql). This ADR records the decisions; that file is the implementation.

---

## Decisions

### 1. Tables

The schema covers nine entities:

| Table | Purpose |
|---|---|
| `person` | The single profile (one row in v1; MRL-compatible `mrl:Person_N`). |
| `account` | All five account types (cash / savings / credit / investment / property). |
| `category` | Hierarchical, dual-sourced category tree. |
| `payee` | Distinct payees, with optional default category. |
| `txn` | Transactions. (`transaction` is a SQL reserved word; `txn` is the chosen alternative.) |
| `lot` | Per-lot holdings for investment accounts (cost basis, quantity). |
| `valuation` | Mark-to-market events for investment and property accounts. |
| `rule` | Auto-categorisation rules (payee pattern → category). v0.2-prio. |
| `import_batch` | Import provenance and statistics. |

### 2. Keys and identifiers

Each row has two identifiers:

- **`id INTEGER PRIMARY KEY`** — internal surrogate key, used for all foreign keys and joins.
- **`iri TEXT UNIQUE NOT NULL`** — MRL-compatible IRI per ADR-006 (`mrl:CashAccount_1`, `mfl:Transaction_<uuid8>`). Stored opaque text. Used for any future RDF export and for MRL integration; not used as a join key.

Internal joins go through `id`; cross-system references travel as `iri`.

### 3. Currency representation

**INTEGER minor units** (pence/cents) for all currency amounts: `account.opening_balance`, `txn.amount`, `valuation.value`, `lot.cost_basis`. Display layer divides by 100; input is multiplied by 100. The Repository layer hides the conversion.

Rationale: floating-point error accumulates over the kinds of operations the app does at scale (running balances, period totals, IRR compounding). Integer math is exact, indexes cleanly, and aggregates with no rounding artefacts. Trade-off accepted: every read and write crosses a conversion boundary, but that boundary lives in one place (the Repository).

**REAL** is used only for genuinely non-currency quantities — `lot.quantity` (e.g. 0.456 shares) and `lot.unit_cost` (price per unit). These do not roll up into the ledger balance and don't compound the way running totals do.

### 4. Categories — hierarchical and dual-sourced

Per the existing memory note on this topic, captured here for completeness:

- `category.parent_id INTEGER NULL REFERENCES category(id) ON DELETE SET NULL` — self-referencing tree, NULL parent means top-level.
- `category.source TEXT NOT NULL CHECK(source IN ('system','user','import'))` — provenance discriminator. Not used for permissions; primarily for reporting and UX.
- `UNIQUE (parent_id, name)` — sibling names must be unique within a parent.
- **All categories are deletable** except the reserved **Uncategorised** root, which serves as the deletion sink for every other category. Enforced at the Repository layer (Python), not via schema constraints, because the carve-out is a single named row and a schema-level check would be more brittle than a code-level guard.
- When any other category is deleted, its referencing transactions are re-pointed to Uncategorised. Implemented in the Repository as a transaction (re-point, then delete) rather than via `ON DELETE CASCADE` / `SET DEFAULT`, because SQLite's `SET DEFAULT` doesn't accept a subquery and the Uncategorised id is configuration not constant.
- Import lookup is by **full path** (root-to-leaf), not leaf name. Source-specific separators (Banktivity `:`, QIF `:`) are parsed in the parser layer; the Repository sees a list of name segments.

### 5. Transaction status and direction

Status is the v0.1 enum, unchanged: `Pending` / `Uncleared` / `Cleared` / `Reconciled`. Stored as `TEXT` with a `CHECK` constraint.

Amount sign carries direction: positive = credit (money in), negative = debit (money out). No separate `direction` column — it would duplicate the sign and create the possibility of disagreement.

### 6. Duplicate detection

Preserved from v0.1:

- `txn.import_hash TEXT NULL` — OFX FITID, or composite MD5 for CSV.
- `UNIQUE INDEX ON (account_id, import_hash) WHERE import_hash IS NOT NULL` — partial unique index; manual entries (NULL hash) are unaffected.

### 7. Indexes

Beyond unique indexes and primary keys:

- `txn(account_id, posted_date)` — the register's natural ordering and the basis for running balance.
- `txn(category_id)`, `txn(payee_id)`, `txn(status)` — filter columns on the register.
- `valuation(account_id, valued_on)` — most-recent-valuation lookup.
- `lot(account_id, symbol)` — per-symbol position roll-up.

Additional indexes will be added by observation, not anticipation.

### 8. Foreign-key behaviour

| Reference | On delete |
|---|---|
| `txn.account_id → account.id` | CASCADE — deleting an account deletes its transactions. |
| `txn.payee_id → payee.id` | SET NULL — payee deletion clears the field. |
| `txn.category_id → category.id` | Handled in code — re-point to Uncategorised. |
| `txn.import_batch_id → import_batch.id` | SET NULL — batches can be pruned without losing transactions. |
| `category.parent_id → category.id` | SET NULL — children become top-level when parent is deleted. |
| `lot.account_id → account.id` | CASCADE. |
| `valuation.account_id → account.id` | CASCADE. |
| `rule.set_category_id → category.id` | CASCADE — rule is meaningless without its target. |

SQLite's `PRAGMA foreign_keys = ON` must be set on every connection; the Repository constructor takes care of this.

### 9. Date and timestamp representation

ISO 8601 strings in `TEXT` columns:

- Dates: `YYYY-MM-DD` (`posted_date`, `valued_on`, `open_date`).
- Timestamps: `YYYY-MM-DD HH:MM:SS` (`created_at`, `imported_at`).

SQLite has no native date type; ISO 8601 sorts lexicographically as chronological, parses cleanly into Python `date`/`datetime`, and round-trips without a binding layer. No use of SQLite's `julianday()` arithmetic — date math is done in Python.

### 10. Schema migration

A single `schema_version` table tracks the applied schema version. A new column or table requires a new migration script under `mfl_desktop/migrations/NNNN_short_description.sql`; the repository runs any missing migrations in order at startup. v1 ships with `0001_initial.sql` containing the contents of `docs/schema.sql`.

---

## Options considered (per area, briefly)

- **Currency representation** — INTEGER minor units (chosen), REAL, TEXT decimal strings. REAL rejected for accumulating float error; TEXT rejected for slow aggregation and lack of index-friendly ordering.
- **Single `transactions` table for all account types** vs. polymorphic per-type tables — single table chosen; the v0.1 model proved this is the right granularity, and per-type tables would multiply join complexity for register-style queries.
- **Lot model: per-lot rows vs. lots derived from buy/sell transactions** — explicit `lot` table chosen. Derivation works for simple cases but breaks down for partial sales, corporate actions, and per-lot cost basis methods (FIFO/LIFO/specific). Explicit lots make IRR/ROI per lot a direct query.
- **Category storage: closure table vs. parent_id adjacency list** — adjacency list (`parent_id`) chosen. Closure tables are faster for descendant queries but require trigger maintenance; SQLite `WITH RECURSIVE` over an adjacency list is fast enough for a personal-finance dataset (thousands of categories at the high end).
- **Uncategorised: real row vs. NULL FK sentinel** — real row chosen by the owner on 2026-06-05. Carve-out (non-deletable) follows from the conjunction with "system categories are deletable."
- **Path conflict on import: auto-create separate vs. prompt vs. merge-by-leaf** — auto-create separate chosen by the owner on 2026-06-05.

---

## Consequences

### Positive
- Register, reporting, and per-lot IRR/ROI are expressible as straightforward SQL with indexes that match the access patterns.
- Currency math is exact; running balances and aggregates never drift.
- Category hierarchy and dual-source are clean; queries scale with `WITH RECURSIVE`.
- The IRI column on every entity preserves cross-app identity for MRL integration without making it the join key inside MFL.
- A repository-level guard on Uncategorised is portable across SQLite versions and easy to test.

### Negative / accepted trade-offs
- Every numeric read/write crosses a `÷100` / `×100` boundary. The Repository hides it; UI never sees the integer form. Accepted.
- `ON DELETE` for categories is handled in code, not by the FK declaration. The trade-off (cross-version portability and the named-row carve-out being explicit) is worth the lost declarative neatness. Repository tests must cover the re-point path.
- Adjacency-list categories require `WITH RECURSIVE` for descendant queries. Categories are not deep enough for this to be a perf concern, but queries are slightly more verbose than against a closure table.
- A single `txn` table for all account types means investment-specific columns (symbol, lot_id reference) live on rows where they don't apply. Sparse columns are fine in SQLite, but the model is less self-documenting. Mitigation: column comments and a clear repository surface.

### Implementation notes (non-binding)
- The repository constructor sets `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`, `PRAGMA synchronous = NORMAL` — standard durable-but-fast settings for a single-user app.
- Seed the system categories (Income/Expense top-levels with the v0.1 subcategories) plus the reserved Uncategorised row in `0001_initial.sql`.
- Money helpers (`pence_to_decimal`, `decimal_to_pence`) live in a single module imported by the Repository only.
