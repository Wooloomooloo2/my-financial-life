# ADR-006 — Instance IRI naming strategy

**Date:** 2026-05-18
**Status:** Accepted

---

## Context

Every user-created instance in the Oxigraph store needs a unique IRI. The naming strategy affects readability in SPARQL, collision risk during bulk import, and consistency with the MRL sister application.

---

## Options considered

### Option 1 — Incrementing integers (MRL pattern)
MRL uses `mrl:ClassName_N` where N is an incrementing integer (e.g. `mrl:Person_1`, `mrl:CashAccount_3`). Human-readable in SPARQL and logs. Safe for low-volume entities.

### Option 2 — UUIDs for all entities (chosen for transactions)
`mfl:Transaction_3f2a1b4c` — collision-free regardless of import volume or concurrency. Less readable but irrelevant for transactions which are queried by property values not by IRI.

### Option 3 — UUID for all entities including shared ones
Rejected for entities shared with MRL (accounts, persons) — MRL uses integers and the two apps must produce compatible IRIs for shared-database mode to work without migration.

---

## Decision

**A hybrid strategy based on entity type:**

| Entity type | Pattern | Example | Rationale |
|-------------|---------|---------|-----------|
| Shared with MRL (Account, Person) | `mrl:ClassName_N` integer | `mrl:CashAccount_1` | Consistent with MRL; human-readable; low volume |
| MFL-specific, high volume (Transaction) | `mfl:ClassName_<uuid6>` | `mfl:Transaction_3f2a1b` | Collision-free at import volume; no sequential scan needed |
| MFL-specific, low volume (Payee, CategoryRule, ImportBatch) | `mfl:ClassName_<uuid6>` | `mfl:Payee_a1b2c3` | Consistent with Transaction pattern; simpler than mixing strategies |
| App settings singleton | `mfl:AppSettings_1` | `mfl:AppSettings_1` | Single instance; integer is clearest |

UUID6 = first 6 hex characters of a UUID4, providing sufficient uniqueness for local single-user data volumes.

---

## Consequences

- Transaction IRIs are generated at import time using `uuid.uuid4().hex[:6]`.
- Account and Person IRIs follow MRL's integer pattern to maintain shared-database compatibility.
- IRI generation logic lives in `app/core/ontology/iri_factory.py`.
- SPARQL queries always filter by property values, never by IRI pattern — so the mixed strategy has no query complexity cost.
