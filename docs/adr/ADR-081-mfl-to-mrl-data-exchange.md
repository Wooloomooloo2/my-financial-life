# ADR-081 — MFL → MRL data exchange: file-based RDF export over the shared ontology

**Date:** 2026-06-16
**Status:** Proposed (to be Accepted when workstream M2 implementation begins). Mechanism, direction, and the authority contract are decided; payload phasing and the serialization detail are settled here with one open sub-decision noted.
**Related:** ADR-001 (original shared triple store — superseded by ADR-009), ADR-005 (ontology strategy — MRL dependency, reference-only for MFL), **ADR-006 (instance IRI naming — now load-bearing for interop)**, ADR-009 (storage engine — the MFL↔MRL boundary is data-exchange, not shared storage), ADR-055 (FX `convert_amount`/nearest-rate), ADR-050 (local-first, cross-platform). Implements **workstream M** of `docs/RELEASE_1.0_BACKLOG.md` (owner decisions 2026-06-16: bundle offering, 1.1 fast-follow, MFL→MRL direction).

---

## Context

MFL (this app — SQLite ledger of accounts/transactions/holdings) and **My Retirement Life (MRL)** (RDF/Oxigraph retirement + tax projection engine) are being sold as a **bundle**. They need to interoperate. ADR-009 already retired the "shared database" idea (ADR-001) in favour of a **data-exchange boundary**; ADR-006 deliberately kept MFL's accounts and person on **`mrl:`-namespaced IRIs** precisely so a future exchange would be cheap. This ADR cashes that in.

**The natural division of labour:** MFL knows the user's financial **reality today** (balances, holdings, currencies, credit limits); MRL owns the **forward-looking assumptions** (growth/interest/dividend rates, drawdown order, retirement age, life expectancy, jurisdictions, tax). The MRL ontology already models the very entities MFL owns the data for — `mrl:Account` and its subclasses, `mrl:Person`, `mrl:IncomeSource`, `mrl:Currency` — and its `mrl:PropertyAsset` class is even annotated *"Activated in v1.0.1 for use by My Finance Life."* MRL anticipated being fed by MFL.

**What exists today:** only the IRI compatibility (accounts/person carry `mrl:Class_N` IRIs). There is **no** RDF/SPARQL/Oxigraph code in `mfl_desktop/` (the legacy Oxigraph dependency was removed).

### Options considered

1. **Shared database / MFL writes into MRL's Oxigraph store.** Rejected — already rejected by ADR-009 (storage coupling), and it requires MRL installed on the same machine, locating its store, and resolving write conflicts. Fragile, and largely impossible under any future App-Store sandbox (K3).
2. **MFL live-reads MRL (or vice-versa) at runtime.** Rejected for the same coupling/locating/sandbox reasons; also forces both apps to be running and version-locked.
3. **Custom JSON/CSV export.** Rejected — it throws away the one asset that makes this integration nearly free: the **shared ontology + aligned IRIs**. MRL is RDF-native; a non-RDF format would need a bespoke importer on the MRL side.
4. **File-based RDF/Turtle export over the shared ontology (CHOSEN).** Decoupled, one-way, user-initiated, works whether or not MRL is installed, survives the 1.2 App-Store sandbox (it's a user-chosen file, not a live read), and loads straight into MRL's Oxigraph.

---

## Decision

**MFL emits a user-initiated, file-based RDF/Turtle snapshot of the user's accounts and person, expressed in the shared `mrl:` ontology, which MRL imports as projection inputs. One-way (MFL → MRL), point-in-time, balances-not-ledger.**

### Direction, shape, and what is *not* exported
- **One-way, MFL → MRL.** Projections flowing back into MFL are out of scope (a later M3 arc).
- **Balances and entities, not the transaction ledger.** MRL projects from *current balances + assumptions*; it has no use for 35k transactions. We export account **balances** (computed by the existing `compute_account_balances` for cash/credit and `compute_account_values` for investment/property), **not** `txn` rows. The `mfl:`-namespaced transactions are never emitted.
- **Point-in-time snapshot.** Each export is "as of" a date (default: today); history/time-series is deferred.

### The authority contract (the crux of a clean two-app boundary)
Because IRIs are shared, MRL matches incoming individuals to its own by IRI and **merges**, not replaces. The contract this ADR defines is **which predicates MFL is authoritative for** — MRL overwrites exactly these on import and leaves everything else (its own planning assumptions) untouched:

**MFL-authoritative predicates:** `mrl:accountName`, `mrl:accountBalance`, `mrl:balanceDate`, `mrl:accountCurrency`, `mrl:isLiability`, `mrl:creditLimit`, `mrl:accountType`, `rdf:type` (the account subclass), `mrl:ownedBy`, `mrl:exchangeRateToBase`, `mrl:exchangeRateDate`; on the person: `mrl:firstName`, `mrl:lastName`, `mrl:baseCurrency`.

**MRL-owned, never emitted by MFL:** `mrl:annualInterestRate`, `mrl:annualGrowthRate`, `mrl:annualDividendRate`, `mrl:reinvestDividends`, `mrl:drawdownPriority`, `mrl:statementDay`, `mrl:dateOfBirth`, `mrl:targetRetirementAge`, `mrl:lifeExpectancy`, `mrl:employmentStatus`, `mrl:residesIn`, `mrl:plansToRetireIn`, `mrl:accountJurisdiction`. These are assumptions or facts MFL does not hold; MFL emitting them would clobber the user's MRL inputs. (The merge logic itself lives in MRL, but the *contract* — this predicate split — is owned jointly and recorded here.)

### Entity mapping

**Person** — emit the existing `mrl:Person_1` IRI; set `mrl:firstName`/`mrl:lastName` (split `person.name`; whole string → firstName if not splittable) and `mrl:baseCurrency` → the matching `mrl:Currency` individual. Nothing else (retirement params are MRL's).

**Account** — emit each account's **stored IRI verbatim** (ADR-006) with:

| Predicate | Source in MFL |
|---|---|
| `rdf:type` | mapped MRL subclass (see below) |
| `mrl:accountName` | `account.name` |
| `mrl:accountBalance` | `compute_account_balances` (cash/credit) / `compute_account_values` (investment/property), account's own currency |
| `mrl:balanceDate` | the export "as of" date |
| `mrl:accountCurrency` | → `mrl:Currency_<CODE>` |
| `mrl:isLiability` | `account.is_liability` |
| `mrl:creditLimit` | `account.credit_limit` (credit cards; omit if null) |
| `mrl:accountType` | → skos concept in `mrlx:AccountTypeScheme` (where one exists) |
| `mrl:ownedBy` | → `mrl:Person_1` |
| `mrl:exchangeRateToBase` / `…Date` | ADR-055 nearest-rate `account_ccy → base_ccy` at the as-of date; **emitted only when account currency ≠ base** (per the ontology's own rule) |

**IRI-class vs rdf:type mismatch (important):** MFL mints some IRIs whose local name has **no matching MRL class** — `mrl:SavingsAccount_N`, `mrl:PropertyAccount_N`, `mrl:VehicleAccount_N` (from `account_types.py` `class_name`). An IRI is an **opaque identifier**, so we keep it verbatim and set the correct `rdf:type` separately:

| MFL type (IRI local class) | MFL family | `rdf:type` emitted | `mrl:accountType` concept |
|---|---|---|---|
| `cash_std` (`CashAccount`) | cash | `mrl:CashAccount` | CashAccountType (standard) |
| `savings_std` (`SavingsAccount`) | cash | `mrl:CashAccount` | CashAccountType (savings) |
| `credit_std` (`CreditCardAccount`) | credit | `mrl:CreditCardAccount` | `mrlx:CreditCardAccountType_Standard` |
| `investment_std` (`InvestmentAccount`) | investment | `mrl:InvestmentAccount` | InvestmentAccountType |
| `property_std` (`PropertyAccount`) | property | `mrl:PropertyAsset` | — (no concept) |
| `vehicle_std` (`VehicleAccount`) | vehicle | `mrl:OtherAsset` | — (MRL `OtherAsset` is post-MVP; MRL may ignore until live) |

A pension in MFL is modelled as an `investment_std` account (ADR-058 R4c), so it exports as `mrl:InvestmentAccount` — fine, since `mrl:PensionAccount` is post-MVP in MRL.

**Currency** — emit a deterministic `mrl:Currency_<ISO4217>` individual (e.g. `mrl:Currency_GBP`) with `mrl:currencyCode`, once per distinct currency in use. Deterministic IRI = stable re-export, idempotent merge.

**Jurisdiction** — **not emitted.** MFL holds no per-account jurisdiction; MRL fills `mrl:residesIn`/`accountJurisdiction` itself. (`mrl:Currency`/`mrl:Jurisdiction` stay decoupled per the ontology.)

### Mechanism (engineering)
- New **Qt-free** module `mfl_desktop/export/mrl_rdf.py` with a pure `build_mrl_turtle(repo, as_of) -> str` (returns Turtle text). No Qt, no SQL outside the Repository — same testability pattern as `fx.py`/`prices.py`/`feeds/`.
- **UI:** `File ▸ Export for My Retirement Life…` → choose a path → write `<name>.ttl`. Remember-the-folder per ADR-077 Track-1 convenience.
- **CLI:** `python -m mfl_desktop.cli export-mrl --out path.ttl` for headless + round-trip testing.
- **Serialization — DECIDED: a small hand-rolled stdlib Turtle writer (no new runtime dependency).** Rationale: the output is small, fully under our control, and the project has deliberately stayed RDF-dependency-free since ADR-009 / removed Oxigraph; adding `rdflib` to the shipped binary is packaging surface we don't need. **Correctness is guaranteed in CI** by parsing the writer's output with `rdflib` (a **test-only** dependency) and asserting the expected triple set, plus a round-trip load into MRL's Oxigraph. ⚠ **Open sub-decision:** if the hand-rolled writer proves fiddly (IRI/string escaping, prefixes), fall back to shipping `rdflib` as a runtime dep — it's pure-Python and guarantees valid Turtle. Decide during M2 implementation.
- **Ontology as a pinned contract:** the export targets a named `mrl-ontology.ttl` version. MFL keeps the reference copy under `docs/ontology/` (ADR-005: never edited from MFL). A shared-ontology change is a coordinated, cross-app change. Add/read `owl:versionInfo` to stamp exports.

### Payload phasing (within workstream M2)
- **Phase 1 (M2 core, the deliverable):** Person (identity + base currency) · Currency individuals · Accounts (the full table above). This is the unambiguous, high-value core that seeds an MRL projection. Ship the export verb, CLI, and tests on this.
- **Phase 2 (follow-on):** `mrl:IncomeSource` derivation — annualised recurring income from `scheduled_txn` income rows (cleanest: a recurring salary schedule → one `IncomeSource` with `incomeAnnualAmount`/`incomeCurrency`/`incomeOwner`/`creditedToAccount`), since MFL income is transaction history, not a declared source; needs judgement on which to include and how to annualise. Plus property metadata (`purchasePrice`/`purchaseDate`) and jurisdiction **if** MFL gains those fields.

---

## Consequences

### Positive
- **Cashes in ADR-006.** The shared IRIs mean MFL's accounts map 1:1 onto MRL individuals with zero matching heuristics — the export is mostly a serialization, not a translation.
- **No new runtime dependency**; headless-testable; consistent with the local-first, Qt-free, dependency-lean posture.
- **Cleanly decoupled:** one-way, user-initiated, file-based — works whether or not MRL is installed, can't corrupt MRL's store, and survives the 1.2 App-Store sandbox (a *live* MRL read would not).
- **Privacy-clean:** the user explicitly produces a file and hands it over; nothing leaves silently. Easy to disclose in the privacy policy (workstream B2/M1).
- **The authority contract** lets MFL update reality without ever clobbering the user's MRL assumptions.

### Negative / trade-offs
- **One-way and snapshot-only.** No projections back into MFL (M3), no balance history. Acceptable: MRL projects forward from current balances.
- **The merge/authority split must be honoured on the MRL side** — outside MFL's control. Mitigation: it's recorded here as a joint contract and covered by the round-trip test.
- **Shared-ontology coupling.** Two apps now share a schema; version drift would break exchange. Mitigation: pin the version, keep the reference `.ttl`, treat ontology changes as coordinated.
- **IRI-class vs rdf:type cosmetic oddity** (`mrl:SavingsAccount_3 a mrl:CashAccount`). Harmless (IRIs are opaque) but worth knowing when reading the Turtle.
- **`IncomeSource` and jurisdiction deferred** — a first export gives MRL accounts + identity only; income must be entered in MRL until Phase 2.
- **`mrl:OtherAsset` (vehicles) is post-MVP in MRL** — exported but possibly ignored until MRL activates it.

### Ongoing responsibilities
- Keep the **MFL-authoritative predicate list** in sync if MFL gains new exportable facts (account notes, property purchase price, etc.) — adding a predicate to the export means adding it to the contract.
- Keep `docs/ontology/mrl-ontology.ttl` current when MRL bumps the ontology version, and bump the export's pinned version in lock-step.
- **ADR-006's IRI-namespace discipline is now load-bearing for interop**, not just future-proofing: minting the wrong prefix on insert would silently break the join. The existing pitfall (#3) stands reinforced.
