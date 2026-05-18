# ADR-005 — Ontology strategy — MRL dependency and MFL extension

**Date:** 2026-05-18
**Status:** Accepted

---

## Context

My Financial Life needs to model currencies, jurisdictions, persons, and a full account hierarchy — all of which are already defined in the My Retirement Life ontology (`mrl:` namespace, `mrl-ontology.ttl`). The long-term goal is for both apps to share a single Oxigraph database so that financial events recorded in MFL feed directly into MRL's retirement projections. The question is how to relate MFL's data model to MRL's existing ontology.

This decision is documented in the MRL repository as ADR-010 — Sister app ontology sharing strategy.

---

## Options considered

### Option 1 — Extract shared concepts to a neutral `mrl-core` library immediately
Create a third repository with a neutral namespace containing shared concepts. Both apps depend on it.

**Rejected** because renaming the existing `mrl:` namespace is a breaking change requiring migration of existing MRL user data. Premature extraction risks building the wrong abstraction before both apps are stable.

### Option 2 — Duplicate the ontology in MFL
MFL defines its own independent ontology that re-models currencies, accounts, and persons.

**Rejected** because duplication guarantees divergence. Two independent definitions of the same concepts make the shared-database goal expensive or impossible.

### Option 3 — MFL loads and extends the MRL ontology (chosen)
MFL ships a copy of `mrl-ontology.ttl` as a read-only bundled asset and loads it alongside `mfl-ontology.ttl` on startup. MFL reuses `mrl:` classes and properties directly — no redefinition. MFL-specific concepts are defined in a new `mfl:` / `mflx:` namespace.

---

## Decision

**MFL loads and extends the MRL ontology. The `mrl:` namespace is the shared foundation.**

### Namespaces

| Prefix | Namespace | Owned by |
|--------|-----------|----------|
| `mrl:` | `https://myretirementlife.app/ontology#` | MRL repository |
| `mrlx:` | `https://myretirementlife.app/ontology/ext#` | MRL repository |
| `mfl:` | `https://myfinanciallife.app/ontology#` | MFL repository |
| `mflx:` | `https://myfinanciallife.app/ontology/ext#` | MFL repository |

### Named graphs

| Named graph IRI | Contents |
|----------------|----------|
| `https://myfinanciallife.app/ontology/graph` | All triples from both TTL files |
| `https://myfinanciallife.app/data/graph` | User instance data |

### MFL-specific classes

| Class | Purpose |
|-------|---------|
| `mfl:Transaction` | Single financial event on an account |
| `mfl:Payee` | Named counterparty entity |
| `mfl:CategoryRule` | Auto-categorisation rule |
| `mfl:ImportBatch` | Import event audit record |
| `mfl:ValuationEvent` | Point-in-time value snapshot for property/investments |
| `mfl:AppSettings` | Global application settings |

### Reused MRL classes (no redefinition)

`mrl:Account`, `mrl:CashAccount`, `mrl:InvestmentAccount`, `mrl:CreditCardAccount`, `mrl:PropertyAsset`, `mrl:Currency`, `mrl:Jurisdiction`, `mrl:Person` — and all associated properties.

---

## Consequences

- MRL repository owns `mrl-ontology.ttl`. Any change to shared concepts is committed there first; the updated file is then copied into MFL's `docs/ontology/`.
- MFL never modifies `mrl-ontology.ttl` at runtime.
- When shared-database mode is implemented (future ADR), both apps can point at the same Oxigraph store. No data migration is required because shared concepts already use `mrl:` IRIs in both apps.
- Extraction to a neutral `mrl-core` namespace is deferred until both apps are stable. A future ADR in both repositories will govern that migration.
