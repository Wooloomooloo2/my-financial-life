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
| [ADR-001](ADR-001-backend-language-and-triple-store.md) | Backend language and triple store | 2026-05-18 | Accepted |
| [ADR-002](ADR-002-frontend-stack.md) | Frontend stack | 2026-05-18 | Accepted |
| [ADR-003](ADR-003-packaging-strategy.md) | Packaging strategy | 2026-05-18 | Accepted |
| [ADR-004](ADR-004-cross-platform-portability.md) | Cross-platform portability approach | 2026-05-18 | Accepted |
| [ADR-005](ADR-005-ontology-strategy.md) | Ontology strategy — MRL dependency and MFL extension | 2026-05-18 | Accepted |
| [ADR-006](ADR-006-instance-iri-naming-strategy.md) | Instance IRI naming strategy | 2026-05-18 | Accepted |
| [ADR-007](ADR-007-data-access-patterns.md) | Data access patterns — quad patterns vs SPARQL | 2026-05-18 | Accepted |

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
