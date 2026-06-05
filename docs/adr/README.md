# Architecture Decision Records

This directory contains the Architecture Decision Records (ADRs) for **My Financial Life**. ADRs capture significant technical and architectural decisions made during the project, including the context that motivated them, the options considered, and the consequences of the decision taken.

---

## What is an ADR?

An Architecture Decision Record is a short document that captures a single architectural decision. Each ADR records:
- **Context** — the situation or problem that required a decision
- **Options considered** — the alternatives evaluated
- **Decision** — what was decided and why
- **Consequences** — the positive outcomes, trade-offs accepted, and ongoing responsibilities

ADRs are written at the time a decision is made and are not retrospective documents. Once accepted, an ADR is not edited to reflect subsequent changes — instead, a new ADR is written that supersedes or amends it.

---

## Index

| ADR | Title | Date | Status |
|-----|-------|------|--------|
| [ADR-001](ADR-001-backend-language-and-triple-store.md) | Backend language and triple store | 2026-05-18 | Partially superseded by ADR-009 |
| [ADR-002](ADR-002-frontend-stack.md) | Frontend stack | 2026-05-18 | Superseded by ADR-008 |
| [ADR-003](ADR-003-packaging-strategy.md) | Packaging strategy | 2026-05-18 | Accepted |
| [ADR-004](ADR-004-cross-platform-portability.md) | Cross-platform portability approach | 2026-05-18 | Accepted |
| [ADR-005](ADR-005-ontology-strategy.md) | Ontology strategy — MRL dependency and MFL extension | 2026-05-18 | Accepted |
| [ADR-006](ADR-006-instance-iri-naming-strategy.md) | Instance IRI naming strategy | 2026-05-18 | Accepted |
| [ADR-007](ADR-007-data-access-patterns.md) | Data access patterns — quad patterns vs SPARQL | 2026-05-18 | Accepted |
| [ADR-008](ADR-008-desktop-ui-framework.md) | Desktop application UI framework | 2026-06-05 | Accepted |
| [ADR-009](ADR-009-storage-engine-for-ledger-data.md) | Storage engine for ledger data | 2026-06-05 | Accepted |
| [ADR-010](ADR-010-transactional-schema-design.md) | Transactional schema design | 2026-06-05 | Accepted |
| [ADR-011](ADR-011-account-delete-policy.md) | Account delete policy — hard delete now, archive reserved | 2026-06-05 | Accepted |
| [ADR-012](ADR-012-payee-name-management-policy.md) | Payee name-management policy — rename rejects on collision, merge is the reassign verb | 2026-06-05 | Accepted |
| [ADR-013](ADR-013-category-management-policy.md) | Category management policy — rename / reparent / merge / delete with explicit rejections over silent cascades | 2026-06-05 | Accepted |
| [ADR-014](ADR-014-category-kind.md) | Category kind — per-row column for income / expense / transfer | 2026-06-05 | Accepted |
| [ADR-015](ADR-015-account-folders.md) | Account folders in the sidebar — flat, display-only grouping with balance roll-up | 2026-06-05 | Accepted |
| [ADR-016](ADR-016-file-save-open-model.md) | File save / open model — auto-commit + Save Copy As snapshots | 2026-06-05 | Accepted |
| [ADR-017](ADR-017-bulk-edit-shape.md) | Bulk edit shape — modal dialog with per-field checkboxes | 2026-06-05 | Accepted |
| [ADR-018](ADR-018-reports-framework-and-first-chart.md) | Reports framework + first chart — Spending Over Time | 2026-06-05 | Accepted |
| [ADR-019](ADR-019-net-worth-report.md) | Net Worth report — three-column Assets / Net Worth / Debts | 2026-06-05 | Accepted |
| [ADR-020](ADR-020-account-transfers.md) | Account transfers — category-driven, two linked transactions sharing one transfer_id | 2026-06-05 | Accepted |
| [ADR-021](ADR-021-csv-mapping-wizard.md) | Generic CSV column-mapping wizard | 2026-06-05 | Accepted |

---

## Summaries

### ADR-001 — Backend language and triple store
Selects **Python** as the backend language and **Oxigraph** (via `pyoxigraph`) as the embedded triple store, consistent with the sister application My Retirement Life. Oxigraph runs inside the Python process with no separate server, has a very low memory footprint suitable for older consumer hardware, and is fully SPARQL 1.1 compliant. The API layer uses **FastAPI**. Consistency with MRL is an explicit goal — both apps share an ontology and are designed to eventually share a database, making stack alignment a strong reason to adopt the same decisions rather than re-evaluate from scratch.

### ADR-002 — Frontend stack
Selects **HTMX + Tailwind CSS + DaisyUI** as the frontend stack, consistent with My Retirement Life. HTMX enables server-driven UI updates from the FastAPI backend without a JavaScript build pipeline. DaisyUI provides a complete component library including built-in light/dark theming. **Chart.js** (CDN) is used for data visualisation. Consistency with MRL is the primary driver; the stack was already validated for this type of application.

### ADR-003 — Packaging strategy
Defines the distribution approach for non-technical end users: **Windows** (PyInstaller → `.exe`), **macOS** (PyInstaller → `.app` bundle), and **Linux** (PyInstaller → AppImage). Consistent with My Retirement Life. A single build toolchain produces all platform targets. Unsigned macOS builds will show a Gatekeeper warning on first launch until code signing is implemented.

### ADR-004 — Cross-platform portability approach
Defines engineering practices that enforce Windows/Linux/macOS portability: `pathlib.Path` for all file path construction, `platformdirs` for OS-appropriate data directories, `.gitattributes` enforcing LF line endings, `python-dotenv` for configuration, and a prohibition on platform-specific runtime dependencies. Consistent with My Retirement Life ADR-004.

### ADR-005 — Ontology strategy — MRL dependency and MFL extension
My Financial Life reuses the My Retirement Life ontology (`mrl:` namespace) as a shared foundation for currencies, jurisdictions, persons, and the full account hierarchy. MFL-specific concepts (transactions, payees, category rules, import batches, valuation events) are defined in a separate `mfl:` namespace in `mfl-ontology.ttl`. Both TTL files are loaded into a single ontology named graph on startup. Extraction to a neutral shared namespace (`mrl-core`) is deferred until both apps are stable. This decision is recorded in MRL as ADR-010.

### ADR-006 — Instance IRI naming strategy
All user-created instance IRIs follow the pattern **`mfl:ClassName_<uuid>`** where the UUID is generated at creation time (e.g. `mfl:Transaction_3f2a1b`). UUIDs are used rather than incrementing integers (as in MRL) because transactions are created at high volume and UUIDs avoid any risk of collision during bulk import. MRL's integer pattern is retained for low-volume entities shared with MRL (accounts, persons) where human-readability in SPARQL is more valuable.

### ADR-007 — Data access patterns — quad patterns vs SPARQL
Establishes a clear split between the two Oxigraph read mechanisms, consistent with MRL ADR-007: **`quads_for_pattern`** is used for fetching all properties of a known IRI and checking existence. **SPARQL SELECT** is used for filtering, aggregation, multi-hop traversal, and reporting queries. All writes use **SPARQL UPDATE** with explicit XSD datatype annotations on numeric, boolean, and date values.

### ADR-008 — Desktop application UI framework
Replaces the v0.1 browser-based frontend with a native desktop UI built on **PySide6 (Qt for Python)**, packaged as a single Windows `.exe` via PyInstaller. macOS and Linux follow. Qt's Model/View architecture (`QAbstractTableModel` + `QTableView`) is purpose-built for the high-performance, editable, virtualised transaction register the application needs and which HTML/HTMX could not deliver to a Banktivity-grade standard. Tauri, Electron, Flutter, and .NET MAUI were considered and rejected — Tauri because it still renders the UI in a WebView and forces either a Rust rewrite of the import engine or a sidecar process; the others because they discard the existing Python parser/import investment. Supersedes ADR-002.

### ADR-009 — Storage engine for ledger data
Replaces Oxigraph as MFL's primary store with **SQLite**, motivated by the actual workload: tens of thousands of transactions, register filter/sort/search/paginate at sub-100 ms, and per-lot IRR/ROI calculations — all materially easier and faster in SQL than SPARQL. **MRL retains Oxigraph** because its workload (tax law across jurisdictions, heterogeneous evolving relationships) is what RDF was designed for. The "shared database with MRL" goal from ADR-001 moves from the storage layer to the integration boundary — MFL can read MRL's store as a reference source and emit RDF for export, but writes its own data relationally. DuckDB is recorded as a future analytical attachment if reports become a bottleneck. Partially supersedes ADR-001.

### ADR-010 — Transactional schema design
Fixes the concrete SQLite schema implementing ADR-009: nine tables (`person`, `account`, `category`, `payee`, `txn`, `lot`, `valuation`, `rule`, `import_batch`), each carrying both an internal `id` and a cross-app `iri` per ADR-006. **Currency stored as INTEGER minor units (pence)** for exact arithmetic; REAL only for non-currency quantities like share counts. **Categories are hierarchical and dual-sourced** (system/user/import), with `parent_id` self-referencing adjacency and `WITH RECURSIVE` for descendant queries. All system-default categories are deletable except the reserved **Uncategorised** row, which serves as the deletion sink (carve-out enforced at the Repository layer). Import path conflicts auto-create as separate categories rather than prompting or auto-merging. Duplicate detection preserved from v0.1 via a partial unique index on `(account_id, import_hash)`. The reference SQL is in [`docs/schema.sql`](../schema.sql).

### ADR-011 — Account delete policy
The desktop app's account-management UI exposes **hard delete only** for v0.1, with a confirmation dialog that names the account, shows the cascaded transaction count, and warns the action cannot be undone. The schema-level `archived_at` column remains **reserved** for a future Archive UX; `Repository.list_accounts` continues to filter it out so adding archive later is additive (Repository methods + sidebar filter, no schema migration). Matches the user's stated request for this milestone; a non-technical user's mental model of "delete" is preserved; cascading is enforced at the FK layer so deletions are never partial.

### ADR-012 — Payee name-management policy
Defines the policy behind the desktop app's payee-management UI. **Rename collisions reject** with an error that directs the user to Merge — auto-merging on rename would silently reassign hundreds of transactions on what looks like a single-row edit. **Merge is the explicit destructive verb**: the user picks 2+ payees, chooses a target — either one of the selected payees or a brand-new name typed at the picker — and confirms a dialog that names the target and states the reassignment count, with the work done atomically in one SQL transaction. Typing a name that matches an existing payee outside the selection is rejected so a single merge never silently pulls in unselected payees. **Delete preserves transactions and clears their payee** via the schema's `ON DELETE SET NULL` FK rule; the confirmation surfaces this consequence explicitly. Same rename / merge / delete shape is reused for category management (ADR-013).

### ADR-013 — Category management policy
Extends the ADR-012 shape to the hierarchical category tree with the **Reparent** verb added. **Rename collisions reject** per-parent (`UNIQUE(parent_id, name)`); the user is directed to Merge. **Reparent rejects cycles** (target in the moved subtree, via `WITH RECURSIVE` descendant query) and **rejects sibling-name collisions** at the new parent. **Merge rejects sources with subcategories** — the cascade-collision morass of moving children with the merged node is avoided by forcing the user to reparent or merge children first. Merge target may be one of the selected categories or a brand-new top-level name; an existing-outside-selection match rejects per ADR-012. **Delete reassigns transactions to the reserved Uncategorised row** (ADR-010 id=1) after a counted confirmation, **rejects Uncategorised itself**, and **rejects categories with subcategories** rather than silently demoting children via `ON DELETE SET NULL`. Every destructive verb stays at one level at a time; deep restructures are a sequence of small reversible steps, not a single click whose effects are hard to predict.

### ADR-014 — Category kind
Adds a **per-category `kind` column** with values `income`, `expense`, `transfer` (CHECK constraint), so reports and cashflow can interpret signed amounts: a positive amount on an expense category reads as a refund; a negative on an income category as a correction; transfers don't count in either bucket. Migration `0002` adds the column, backfills via recursive CTE (Income subtree → income; everything else → expense), and seeds `Transfer` as the third reserved top-level system root. **New top-level categories pick their kind**; **sub-categories inherit the parent's kind**. **Reparenting across kinds is an explicit confirmation** — confirming cascades the new kind to all descendants. **Merging across kinds is rejected** up front so the user converts kind first via reparent. Uncategorised defaults to `kind='expense'`; reports should surface its transactions as "needs categorising" separately rather than rolling them into the refund total. Import-created top-level categories default to `expense`. Per-row column chosen over derived-from-root because it matches the Banktivity mental model the owner is coming from and avoids forcing a migration of every existing top-level non-system category under a kind root.

### ADR-015 — Account folders in the sidebar
Replaces the flat `QListWidget` sidebar with a two-column `QTreeWidget` (Name | Balance) so accounts can be grouped into Banktivity-style folders. Migration `0003` adds an `account_folder` table (id, name, sort_order, archived_at) and a nullable `account.folder_id` FK with `ON DELETE SET NULL`. Folders are **flat** (no nesting) in v1 — nesting is an additive future migration. Folders are **display-only** — clicking a folder toggles expansion; only accounts and "All transactions" change the register view. Folder-as-view-mode is recorded in the backlog. Balance computation is `opening_balance + SUM(txn.amount)` per account in one query; folder sums are computed in the sidebar, **summed naively across currencies** (documented limitation). Folder verbs: New, Rename, Delete (accounts inside fall to root), Move Up/Down (swap sort_order with neighbour), plus Move Account → Folder ▸ submenu on the account context menu. Account reordering inside folders is deferred per the owner's steer.

### ADR-016 — File save / open model
**No plain Save** menu item — every Repository commit already hits disk, so a "Save" verb would be a no-op and mislead. Two file verbs only: **File → Save Copy As… (Ctrl+Shift+S)** writes an atomic SQLite backup of the current DB to a chosen path via `sqlite3.Connection.backup()` (online + WAL-safe); **File → Open… (Ctrl+O)** closes the current Repository, opens a new one against the chosen file (migration runner upgrades older backups to the current schema), and rebuilds sidebar + category cache + filter combo + register model in one swap. Window title shows the current filename so the user can't accidentally edit the wrong file when juggling backups. File extension: `.mfl` preferred, `.db` accepted on Open; Save Copy As defaults to `.mfl` when the user doesn't type an extension. Document-style dirty tracking is rejected — autosave is universally preferable for financial data, and matching the on-disk-is-truth contract is correct given how Repository writes already work.

### ADR-017 — Bulk edit shape
The register's bulk-edit verb is a **modal dialog with per-field checkboxes**: Payee, Category, Status, Memo. Each row has a leading checkbox that enables its editor; only checked fields are applied to the selection. Empty Payee or Memo with the box ticked **clears** that field on every selected row. Category and Status are NOT NULL, so they have no clear semantics — the user must pick a value. One repository call (`bulk_update_transactions` with an `_UNSET` sentinel that distinguishes "don't change" from "clear to None") runs every checked field's UPDATE inside a single SQL transaction; failure mid-way rolls back the whole batch. Triggers: Transaction menu → Bulk Edit Selected… (**Ctrl+E**) and the table's right-click menu (entry only appears when ≥2 rows are selected). Single-row edits stay on the existing inline editor — the bulk dialog would be noisier for one row. Bulk bar (persistent inline editor strip) and per-field context-menu actions were rejected: bulk bar mixes "what's selected" with "what's editable" in one UI region, and per-field actions break the atomic-across-N-rows feel by requiring one prompt per field.

### ADR-018 — Reports framework + first chart: Spending Over Time
Reports open from a new top-level **Reports** menu into **non-modal `QMainWindow`s**, single-instance per report (re-clicking raises the existing window). Chart library is **QtCharts** (bundled with PySide6 — no extra dep). The first report is **Spending Over Time**: stacked bar chart, granularity = weekly / monthly / quarterly / annually, configurable date range, account filter, category-group filter, Include Uncategorised toggle, dashed average line drawn across the chart, and a summary strip showing total + average + bucket count. Defaults: Monthly, last 12 months, all accounts, all groups, Uncategorised included. Spending semantics: **strict outflow** — `SUM(-amount)` on `kind='expense'` rows where `amount < 0`. Net-spending (refunds reduce) was considered and rejected for v1 because Uncategorised's `kind='expense'` default (ADR-014) misclassifies imported income as refunds, producing negative bar segments that don't render cleanly. Refund handling moves to the future Cash Flow report. **Pie charts are explicitly out** (owner rule). Grouping rule for stack segments: a transaction's category rolls up to its **deepest ancestor whose parent is a root** — so `Expense → Groceries → Tesco` groups as `Groceries`. Uncategorised is its own group with its own toggle. Auto-refresh on every control change (no Refresh button) — at ~1.3k rows the SQL is well under a frame. Average line uses an invisible second `QValueAxis` on the bottom because mixing categorical-x bars and numeric-x lines in one `QChart` needs two x-axes sharing one y-axis.

### ADR-019 — Net Worth report
Three-column non-modal `QMainWindow` modelled on Pocketsmith's Net Worth view, opened from **Reports → Net Worth…**. **Summary** (left): big net-worth total, a horizontal **`ProportionalBar`** showing asset-family widths (no pie — owner rule), colour-coded legend. **Assets** (middle): green header + total, `QTreeWidget` grouped by `account.family` (Cash & Bank, Investments, Property), each group expanded with its individual accounts; "+ Asset" button at the bottom. **Debts** (right): red mirror — liability balances stored negative in the DB are displayed as positive amounts (£429 owed, not -£429); "+ Debt" button. Family → label/colour mapping is a small static table; adding a new family means adding a row there. Investment / property balances in v1 still use `opening_balance + sum(txns)` — the schema's `valuation` table is reserved for mark-to-market but isn't wired yet. Add Account opens the existing `AccountDialog` (same dialog from both + buttons, no preset) and refreshes the register's sidebar after creation.

### ADR-020 — Account transfers
A transfer is **one user intent realised as two linked `txn` rows** — outflow on the source, inflow on the destination — paired by a shared `transfer_id TEXT` column (`mfl:Transfer_<uuid8>` per ADR-006). Migration `0004` adds the column and a partial index. The **verb is the category**: there is no dedicated New Transfer menu item; picking a `kind='transfer'` category on any creation/edit flow (New Transaction, inline cell edit, Bulk Edit) prompts for the destination account and creates the partner row. **`create_transfer`** handles fresh transfers from New Transaction; **`convert_to_transfer`** handles existing-row → transfer-half conversion (inline edit); **`bulk_set_category_and_convert`** handles bulk-edit batches atomically. Direction is encoded in the source row's signed amount — partner gets the opposite sign — so the same conversion logic works regardless of whether the user is entering an outflow or an inflow. **Delete is partner-aware**: `delete_transactions` expands the selection via `expand_transfer_partners` so the user can't strand half a transfer. Payee convention: `create_transfer` sets both halves to "Transfer to/from {account}"; `convert_to_transfer` leaves the source's existing payee alone (preserves import-derived payees) and only the new partner gets the convention payee. Edit-sync, auto un-transfer on category change away from transfer kind, and import-side transfer detection are all deferred to future revisions.

### ADR-021 — Generic CSV column-mapping wizard
Closes the open hole in CSV import: when `_detect_format` returns `"generic"` (Pocketsmith, generic bank CSVs, anything that isn't Banktivity/credit-card/OFX/QFX), a new single-screen modal `CsvMappingDialog` lets the user map Date / Amount / Payee / Memo / Category to columns in their file. **Smart-default pre-fill** scans the file's headers against the alias lists already in `csv_parser.py` (`_DATE_ALIASES` etc., plus a new `_CATEGORY_ALIASES`) so conventional Pocketsmith-style files open with all combos already set — the user just confirms. **Amount style** is a radio toggle between *Single signed column* (with an `Invert sign` checkbox for credit-card-style positive=debit exports) and *Separate debit and credit columns*. **Date format** offers `auto` plus named strptime presets plus a free-text custom. **Live after-mapping preview** re-parses the staged 5 preview rows on every widget change and shows `(unparseable)` cells in red so date-format mismatches and inverted-sign confusions are visible before commit. The dialog is **purely value-producing** — accepts → caller reads `.mapping`, rejects → caller calls the new `ImportService.discard_pending_map(token)` — and `register_window._on_import` now routes the mapped result through the same `_commit_pending()` helper as known-format imports, so the silent-commit-and-status-bar pattern (per the no-dialog-for-known-imports feedback) holds once the format is understood. **No schema change**; no new tables. **Saved mapping profiles are explicitly deferred** — auto-detecting a repeat Pocketsmith CSV by header signature and skipping the dialog is its own future ADR, not bolted onto this one.

---

## Status values

| Status | Meaning |
|--------|---------|
| **Proposed** | Under discussion; not yet decided |
| **Accepted** | Decision made and adopted; the approach described is in effect |
| **Implemented** | Accepted and fully built; implementation notes may record divergences |
| **Superseded** | Replaced by a later ADR; kept for historical reference |
| **Deprecated** | No longer applicable but not replaced by a specific decision |

---

## Adding a new ADR

1. Copy the filename pattern: `ADR-NNN-short-description-of-decision.md`
2. Use the next available number in sequence
3. Fill in Context, Options considered, Decision, and Consequences
4. Set status to `Proposed` until the decision is agreed
5. Add a row to the index table and a summary paragraph above
6. Once agreed, update status to `Accepted`
