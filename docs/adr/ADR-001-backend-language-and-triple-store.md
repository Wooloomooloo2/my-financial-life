# ADR-001 — Backend language and triple store

**Date:** 2026-05-18
**Status:** Accepted

---

## Context

My Financial Life requires a backend language, web framework, and persistent data store. The application must run locally on Windows, macOS, and Linux without requiring a separate database server process. It must also be compatible with the sister application My Retirement Life, with which it will eventually share a database.

---

## Options considered

### Option 1 — Python + FastAPI + Oxigraph (chosen)
Identical stack to My Retirement Life. Python is well-suited to data-centric applications, has strong library support for RDF and OFX parsing, and is familiar to a wide contributor base. FastAPI provides async routing, automatic OpenAPI documentation, and clean dependency injection. Oxigraph runs embedded in the Python process with no separate server, has a low memory footprint, and is fully SPARQL 1.1 compliant.

### Option 2 — Python + FastAPI + SQLite
Would replace Oxigraph with a relational store. SQLite is excellent for transaction data but does not natively support the graph data model required for the shared ontology. Migration to a shared database with MRL would require a schema translation layer.

### Option 3 — Different language or framework
Rejected immediately. Stack consistency with MRL is an explicit goal — both apps share an ontology and are designed to eventually share a database. Diverging on the stack would create unnecessary complexity.

---

## Decision

**Python 3.13 + FastAPI + Oxigraph (pyoxigraph), consistent with My Retirement Life.**

---

## Consequences

- The application runs as a single Python process with no external dependencies beyond the Python runtime.
- Oxigraph data is stored on disk in the OS-appropriate user data directory via `platformdirs`.
- SPARQL 1.1 is available for all query and update operations.
- Stack consistency with MRL means developers familiar with one app can contribute to the other.
- Future shared-database mode requires no data migration — both apps already use the same store format and ontology namespace conventions.
