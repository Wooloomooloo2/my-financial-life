# ADR-009 — Storage engine for ledger data

**Date:** 2026-06-05
**Status:** Accepted
**Supersedes (partially):** ADR-001 (Backend language and triple store) — Python remains; Oxigraph is replaced as MFL's primary store. MRL retains Oxigraph.

---

## Context

ADR-001 selected Oxigraph (`pyoxigraph`) as MFL's primary data store, motivated by sharing storage with My Retirement Life and reusing a single ontology across both applications. Building on that foundation through v0.1 has clarified MFL's actual workload:

- Tens of thousands of transactions per user across multiple accounts, growing indefinitely.
- The register must filter, sort, search, and paginate this data with sub-100 ms response.
- Reporting workloads include per-lot IRR and ROI calculations over investment holdings, period-over-period spending comparisons, and category breakdowns over arbitrary date ranges.

This is a transactional ledger workload with analytical reporting on top. SPARQL over a triple store handles it correctly but at a meaningful performance and authoring cost — every aggregation is multi-hop, every running balance becomes a window-function workaround, and per-lot IRR over thousands of rows is unattractive in SPARQL.

My Retirement Life, by contrast, models tax law across multiple jurisdictions with deep, heterogeneous, evolving relationships — the case RDF was designed for. The owner has confirmed MRL will remain RDF-based for this reason.

The original "share a database with MRL" motivation can be satisfied at the integration layer rather than the storage layer.

---

## Options considered

### Option 1 — SQLite for the ledger, RDF retained for MRL bridge only (chosen)

SQLite stores accounts, transactions, lots, valuations, categories, payees, rules, and import batches in a normalised relational schema. Per-lot IRR/ROI is straightforward SQL with window functions. Register operations become indexed B-tree lookups. SQLite ships with Python's stdlib, so no new runtime dependency is introduced and the packaged binary stays small.

The shared conceptual model with MRL is preserved at the **identifier level** — MRL-style IRIs (e.g. `mrl:CashAccount_1`) are stored as opaque text keys on SQLite rows, so cross-app references remain meaningful. MFL ↔ MRL integration happens at boundaries: MFL can read MRL's Oxigraph store directly for reference data (tax rates, jurisdictions), and can emit or consume RDF for full data exchange when needed.

### Option 2 — DuckDB instead of SQLite

DuckDB is a columnar analytical engine, embedded, single-file. Stronger than SQLite on the IRR/ROI/reporting workload (window functions over millions of rows are its specialism). Weaker than SQLite for the OLTP-style writes the register and import workflows do. For a single-user personal-finance application, SQLite's transactional sweet spot is the right starting point; DuckDB can be attached later for analytical queries without replacing SQLite if and when reports become a bottleneck.

### Option 3 — Keep Oxigraph

Rejected. SPARQL aggregations over tens of thousands of transactions, per-lot IRR calculations, and the register's filter/sort/search workload are all materially harder and slower than the equivalent SQL. The original "shared store with MRL" motivation is achievable without shared storage.

### Option 4 — PostgreSQL or another server-backed RDBMS

Rejected. Requires a separate process and installation step, violates the single-binary distribution model (ADR-008), and provides no advantage at single-user, single-machine scale.

---

## Decision

**SQLite as MFL's primary persistent store** for accounts, transactions, lots, valuations, categories, payees, rules, and import batches. **MRL retains Oxigraph** as its primary store. **MFL ↔ MRL integration** happens at the data-exchange boundary, not at the storage layer — MFL can read MRL's store as a reference source and emit RDF for export, but writes its own data relationally.

DuckDB is recorded as a future option for analytical queries; it will not be adopted until a concrete reporting bottleneck demands it.

---

## Consequences

### Positive
- Register filter, sort, search, and pagination become indexed SQL — straightforward and fast.
- Per-lot IRR and ROI are expressible in SQL with window functions.
- SQLite is in the Python stdlib — one fewer dependency to bundle.
- The existing import engine (CSV/OFX/QFX parsers, duplicate detection, staging) is unaffected by the storage swap — only the persistence layer at the bottom needs rewriting.
- MRL's RDF model continues to do what it does best.

### Negative / accepted trade-offs
- The "shared database with MRL" goal from ADR-001 is no longer met at the storage layer; integration moves to an explicit boundary. Acceptable given the two applications model fundamentally different workloads.
- The existing MFL ontology (`mfl-ontology.ttl`) is no longer a runtime artefact for MFL — it remains a domain-model reference document and is reused when MFL emits RDF for MRL exchange.
- Migration of any v0.1 data captured in the Oxigraph store to the new SQLite schema is a one-off task. Given v0.1 is owner-only and the dataset is small, a simple migration script run before the desktop client ships is sufficient.

### Implementation notes (non-binding)
- Initial schema sketch: `account`, `transaction`, `lot`, `valuation`, `category`, `payee`, `rule`, `import_batch`. Foreign keys, `CHECK` constraints for status and category enums, and indexes on `(account_id, posted_date)` and `payee_id`.
- Identifier columns store MRL-compatible string IRIs (e.g. `'mrl:CashAccount_1'`) for any entity that needs cross-app reference.
- A repository layer in Python isolates schema knowledge from the service and UI layers — important for future DuckDB attachment without disturbing the rest of the codebase.
