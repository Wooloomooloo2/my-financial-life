# ADR-007 — Data access patterns — quad patterns vs SPARQL

**Date:** 2026-05-18
**Status:** Accepted

---

## Context

Oxigraph exposes two mechanisms for reading data: quad pattern matching (`quads_for_pattern`) and SPARQL SELECT/UPDATE. Choosing the right tool for each access pattern avoids subtle bugs, particularly around datatype matching, and keeps the codebase consistent. This decision mirrors MRL ADR-007.

---

## Options considered

### Option 1 — SPARQL for everything
Consistent and expressive, but SPARQL literal matching can fail silently when XSD datatypes don't match exactly (e.g. filtering `xsd:decimal` with a plain literal). Verbose for simple property lookups on a known IRI.

### Option 2 — Quad patterns for everything
Fast and reliable for simple lookups, but SPARQL is significantly more concise and powerful for filtering, aggregation, sorting, and multi-hop queries. Writing transaction filtering and reporting queries in quad patterns would be impractical.

### Option 3 — Split by use case (chosen)
Use the right tool for each job. Consistent with MRL ADR-007.

---

## Decision

**A clear split between quad pattern matching and SPARQL:**

### Use `quads_for_pattern` for:
- Fetching all properties of a known IRI (e.g. loading a transaction by its IRI)
- Checking whether an instance exists
- Iterating all instances of a class for low-volume entities

### Use SPARQL SELECT for:
- Filtering transactions by date range, account, category, status, or payee
- Aggregation (sum of spending by category, balance totals, income vs expenditure)
- Multi-hop traversal (transactions → payee → default category)
- Dashboard and reporting queries
- Duplicate detection hash lookups

### Use SPARQL UPDATE for:
- All writes — always with explicit XSD datatype annotations on numeric, boolean, and date values

---

## Consequences

- Datatype annotation is mandatory on all writes. A write helper in `app/data/write_helpers.py` enforces correct types for `xsd:decimal`, `xsd:date`, `xsd:boolean`, and `xsd:integer`.
- `quads_for_pattern` reads are reliable regardless of how literals were stored, making them safe for property fetching even if a future write inadvertently omits a datatype.
- SPARQL queries are collected in module-level constants or dedicated query files — not constructed by string concatenation at call time, to prevent injection and aid readability.
- Both named graphs are queried explicitly in SPARQL: `GRAPH <https://myfinanciallife.app/ontology/graph>` for ontology lookups and `GRAPH <https://myfinanciallife.app/data/graph>` for instance data.
