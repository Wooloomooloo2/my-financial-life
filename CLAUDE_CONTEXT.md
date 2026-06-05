# CLAUDE_CONTEXT.md
# My Financial Life — Developer Context for AI Assistance

This file gives a new Claude session full context to continue development
without needing the original conversation transcript.

**Last updated:** 2026-06-05 — post architecture pivot (ADR-008, ADR-009).

---

## Status at a glance

- **v0.1 shipped** as a local web app (FastAPI + HTMX + Oxigraph). MVP complete, owner-only. Now in maintenance mode.
- **2026-06-05 pivot:** MFL is being rebuilt as a **native desktop application** (PySide6 + SQLite) for Windows-first distribution. See [ADR-008](docs/adr/ADR-008-desktop-ui-framework.md), [ADR-009](docs/adr/ADR-009-storage-engine-for-ledger-data.md), and [ADR-010](docs/adr/ADR-010-transactional-schema-design.md).
- **Desktop app under `mfl_desktop/` is the live target.** Multi-account register with an All-transactions cross-account view, OFX/QFX/CSV import working end-to-end through the Qt UI, layered architecture (UI → proxy → model → Repository → SQLite). Owner has loaded six months of real data into it (~1,300 transactions) and confirmed the feel.
- **Original PySide6 prototype kept at `prototype_register/`** as a reference for the data-grid pattern; it's not the main app.
- **Sister app MRL stays RDF-based.** MFL ↔ MRL integration is now at the data-exchange boundary, not shared storage.

---

## Owner profile

Mark Hall (`mark.a.hall@gmail.com`). Sole developer, not a professional developer, comfortable reading and editing Python. Runs the app locally; copies code into files manually. Moving personally from macOS to Windows — hence Windows-first distribution. Will share the packaged app with non-technical friends/family.

**Durable working preferences:**
- Deliver **complete files**, not diffs or snippets. After full-file replacements, verify imports were preserved.
- **Every significant architectural decision is recorded as an ADR** under `docs/adr/`, with rationale for the alternatives rejected — even decisions the owner has explicitly delegated.

---

## Target architecture (post-pivot, in progress)

**Stack:** Python 3.13 + **PySide6 (Qt for Python)** + **SQLite**, with DuckDB attachment as a future option for analytical queries.

**Distribution:** Single-file Windows `.exe` via PyInstaller. macOS and Linux follow once Windows is stable.

**Layering** — mirrored in the prototype, will carry forward into the real app:

```
QMainWindow                ← top-level windows + navigation
    │
    ▼
QSortFilterProxyModel      ← filter / sort
    │
    ▼
TransactionTableModel      ← QAbstractTableModel contract
    │
    ▼
Repository                 ← only layer that touches SQL
    │
    ▼
SQLite
```

The Repository contract isolates the rest of the codebase from the storage decision. Future DuckDB analytics attachment slots in at the Repository level without disturbing UI or service code.

---

## Legacy architecture (v0.1, maintenance only)

**Stack:** FastAPI + Jinja2 + HTMX + Tailwind/DaisyUI + Chart.js + pyoxigraph 0.5.8.

**Run:** `python main.py` from the project root → `http://127.0.0.1:8000`.

**Location:** `main.py` + everything under `app/`. Jinja templates under `app/templates/`. TTL ontology under `docs/ontology/`.

**Status:** No new features. Bug fixes only if the owner needs them while the rewrite is in flight. The import engine (`app/core/import_engine/`) will be **lifted** onto the new repository — preserved, not rewritten.

---

## What survives the rewrite

| Element | Status in rewrite |
|---|---|
| CSV / OFX / QFX parsers (`app/core/import_engine/`) | Preserved; retargeted at the SQLite repository |
| Duplicate detection (OFX FITID hash, CSV composite MD5 hash) | Preserved |
| Account types and families (cash / credit / investment / property) | Preserved |
| Transaction status enum (Pending / Uncleared / Cleared / Reconciled) | Preserved |
| Category taxonomy | **Changed** — now hierarchical and dual-sourced (see below) |
| MRL-compatible IRIs as identifiers | Preserved — stored as opaque text columns in SQLite |
| MFL ontology TTL (`mfl-ontology.ttl`) | Becomes a reference document; no longer a runtime artefact |
| HTMX templates / Jinja layouts | Discarded — replaced by Qt widgets |
| Pagination logic | Discarded — `QTableView` virtualises natively |
| SPARQL data-access code | Discarded for MFL (retained inside MRL only) |

---

## Domain reference (still authoritative)

### Account types and families

| Key | Label | Family | Liability | Balance from |
|---|---|---|---|---|
| `cash_std` | Current account | cash | No | Transactions SUM |
| `savings_std` | Savings account | cash | No | Transactions SUM |
| `credit_std` | Credit card | credit | Yes | Transactions SUM |
| `investment_std` | Investment account | investment | No | Latest valuation |
| `property_std` | Property | property | No | Latest valuation |

### Transaction statuses

| Status | Usage |
|---|---|
| Pending | Manual entry, not yet on statement |
| Uncleared | Imported, needs review |
| Cleared | Verified correct |
| Reconciled | Matched to closing balance |

Import default behaviour: first import on an empty account → `Cleared` (treated as historical load); subsequent imports → `Uncleared` (review). Banktivity CSV imports always honour the per-transaction status from the file.

### Categories (revised model)

Categories form a **tree** with three sources:

1. **System defaults** — seeded from the v0.1 taxonomy (Income / Expense top-level, with the v0.1 subcategories). Not deletable; archivable/hidden TBD.
2. **User-created** — the user can add categories anywhere in the tree at any time.
3. **Import-created** — when an import file carries a category MFL doesn't recognise, the category is auto-created. For source formats with hierarchical paths (Banktivity uses `Parent:Child`), the path is parsed and intermediate nodes are created as needed.

Import lookup is by **full path**, not just leaf name — `Auto:Gas` and a top-level `Gas` are distinct categories. Reports/filters use SQLite `WITH RECURSIVE` to walk the tree when "this category and descendants" is requested. Open design questions tracked in memory (`project-categories-design`) and will be settled in the schema ADR.

### Identifier conventions

Carried forward from ADR-006:

- **Accounts and Person:** `mrl:ClassName_N` (integer suffix), MRL-compatible — e.g. `mrl:CashAccount_1`, `mrl:Person_1`.
- **Transactions, ImportBatches, ValuationEvents:** `mfl:ClassName_<uuid8>` — e.g. `mfl:Transaction_a3f7c901`.

In SQLite, these are stored as `TEXT` columns on the relevant rows (e.g. `account.iri`, `txn.iri`). They remain meaningful for any future RDF export and for MRL integration; internally the database uses INTEGER PKs for joins.

### Import workflow (preserved)

```
Upload (OFX / QFX / CSV)
    ↓
parse_and_stage() → returns (token, "preview" | "map")
    ↓ if "map"
Column mapping UI            ← for unknown CSV layouts
    ↓
apply_mapping_and_stage()
    ↓
Preview                      ← new / duplicate / potential-match classification
    ↓
commit_import()
    ↓
Result summary
```

**Duplicate detection:**
- OFX: bank FITID stored as `import_hash`.
- CSV: `MD5(account_iri + "|" + date + "|" + amount + "|" + payee_raw)[:12]`.

**Potential match:** manual entry on same account, same amount, same direction, date ±2 days. Default action is merge; merge preserves user data and adds the import hash to the manual entry.

### CSV format detection (preserved)

`_detect_format(lines)` returns `"banktivity"`, `"creditcard"`, or `"generic"`.

- **Banktivity:** Row 1 = account name (≤2 commas), row 2 has Type/Status/Date/Payee headers. Per-transaction status honoured. Amounts have £ symbol and commas. Date format M/D/YY. Split transactions collapsed to parent total. Transfers imported as debit. **Categories use `:` as the hierarchy separator** and will be created if unknown.
- **Credit card:** headers contain `debitCreditCode` or `merchant.name`. Date is ISO 8601 with time. Amount is always positive; direction comes from `debitCreditCode`.
- **Generic:** falls through to the column-mapping UI.

---

## Sister app — My Retirement Life (MRL)

MRL remains RDF/Oxigraph-based — its workload (tax law across jurisdictions, heterogeneous evolving relationships) is what RDF was designed for. Integration with MFL now happens at the **data-exchange boundary**, not shared storage:

- **Identifier compatibility** — MFL stores MRL-style IRIs as opaque text on rows that need to refer to MRL entities.
- **Reference reads** — MFL can read MRL's Oxigraph store directly for reference data (tax rates, jurisdictions).
- **Data exchange** — MFL can emit and consume RDF when full data exchange is needed.

The original "shared database with MRL" goal from ADR-001 is no longer the strategy at the storage layer. See ADR-009.

---

## Repository structure

```
C:\Users\hallm\Documents\GitHub\my-financial-life\
├── main.py                          # v0.1 entrypoint (legacy)
├── requirements.txt                 # v0.1 deps (legacy)
├── CLAUDE_CONTEXT.md                # this file
├── docs/
│   ├── adr/                         # ADR-001 through ADR-010 + README
│   ├── schema.sql                   # ADR-010 reference schema (copy lives in mfl_desktop/migrations/)
│   └── ontology/
│       ├── mrl-ontology.ttl         # shared with MRL — do not edit from MFL
│       └── mfl-ontology.ttl         # MFL-specific, now a reference document
├── app/                             # v0.1 web app (legacy — maintenance only)
│   ├── api/                         # FastAPI routes
│   ├── core/
│   │   ├── accounts/
│   │   ├── dashboard/
│   │   ├── import_engine/           # SUPERSEDED — port lives at mfl_desktop/import_engine/
│   │   ├── ontology/
│   │   ├── transactions/
│   │   └── templates.py
│   ├── data/                        # Oxigraph singleton + loader
│   └── templates/                   # Jinja/HTMX (discarded in rewrite)
├── mfl_desktop/                     # Native desktop app — the live target
│   ├── __main__.py                  # `python -m mfl_desktop` launches the GUI
│   ├── cli.py                       # init / import / list / categories / add-account
│   ├── requirements.txt             # PySide6 + ofxtools
│   ├── README.md
│   ├── db/
│   │   ├── money.py                 # Decimal ↔ INTEGER pence
│   │   ├── repository.py            # Repository — the ONLY layer that touches SQL
│   │   └── schema.py                # Migration runner (manages schema_version)
│   ├── migrations/
│   │   └── 0001_initial.sql         # ADR-010 schema + seeded categories
│   ├── import_engine/               # Lifted from app/core/import_engine/
│   │   ├── ofx_parser.py            # OFX/QFX — verbatim from v0.1
│   │   ├── csv_parser.py            # Banktivity / credit-card / generic CSV (syntax bug fixed)
│   │   └── import_service.py        # Stage + classify + commit, rewritten against Repository
│   └── ui/
│       ├── register_window.py       # QMainWindow with sidebar + register
│       ├── register_model.py        # QAbstractTableModel — single-account + all-transactions modes
│       ├── filter_proxy.py          # Sort / filter on underlying values
│       ├── delegates.py             # Category + Status combo delegates
│       └── sidebar.py               # Account list with "All transactions" entry
└── prototype_register/              # Original PySide6 prototype — kept for reference
    ├── README.md
    ├── requirements.txt
    ├── seed.py                      # builds prototype.db (10k synthetic txns)
    └── register_proto.py            # single-window register demo
```

---

## Backlog

Captured during the rewrite as features are deferred for later turns.

### Register UX (deferred from initial PySide6 wire-up, 2026-06-05)

The register window currently uses standard `QComboBox` delegates for category and status, and a plain `QLineEdit` for payee. Real use surfaces four UX improvements:

1. **Payee autocomplete on edit.** Typing in the Payee cell should suggest existing payees from the `payee` table (Qt `QCompleter` over `QLineEdit`). Faster than free typing, ensures consistent names.
2. **Category autocomplete on edit.** Same pattern for category. A flat combo of 100+ categories becomes unusable; a typeahead is the right shape regardless of dataset size.
3. **Inline category creation while editing.** If the user types a category name that doesn't exist, an option to create it on the spot (default placement: top-level, source = `user`; re-parent later via category management). Drives off the same typeahead widget as (2).
4. **Multi-edit / bulk-edit.** Select N transactions in the register, set one or more fields (category / status / payee / memo) for all of them in a single action. The v0.1 web app had this as a "bulk bar"; the Qt version probably belongs in a sidebar or modal that opens when more than one row is selected.

Items (1)–(3) cluster around a single custom typeahead delegate and should be done together. (4) is a separate piece of work.

### Other deferred items

- **Generic-CSV column mapping UI.** When `parse_and_stage` returns `"map"` the GUI currently shows a "coming soon" message. Build the mapping UI on top of `apply_mapping_and_stage` so unknown bank-CSV formats can be imported with user-supplied column mappings. (Known formats — Banktivity, credit-card, OFX/QFX — already commit silently per the no-dialog feedback rule; the mapping UI is *only* for genuine unknowns.)
- **QIF parser.** v0.2 high-priority; lift the QIF format alongside the existing OFX/CSV parsers.
- **Categorisation rules engine.** Schema reserves the `rule` table; no service uses it yet.
- **Per-lot IRR / ROI.** Schema is in place (`lot`, `valuation`); no computation yet.
- **Category management UI.** Re-parent, rename, archive categories — needed to manage what import-created and to undo path-conflict separations after the fact.
- **Dashboard.** v0.1 had it; needs porting to Qt with charts (QtCharts or pyqtgraph).
- **Account / settings management UI.** Add / edit / archive accounts, set base currency.
- **Packaging.** Single-file `.exe` via PyInstaller per ADR-008.

---

## Architecture Decision Records

| ADR | Topic | Status |
|---|---|---|
| ADR-001 | Backend language and triple store | Partially superseded by ADR-009 |
| ADR-002 | Frontend stack (HTMX) | **Superseded by ADR-008** |
| ADR-003 | Packaging (PyInstaller / AppImage) | Applies (Windows-first prioritised) |
| ADR-004 | Cross-platform portability | Applies |
| ADR-005 | Ontology strategy (MRL dependency) | Applies (reference-only for MFL) |
| ADR-006 | Instance IRI naming | Applies (IRIs become text keys in SQLite) |
| ADR-007 | Data access patterns (quad vs SPARQL) | Legacy-code only |
| ADR-008 | Desktop UI framework — PySide6 | **Accepted 2026-06-05** |
| ADR-009 | Storage engine — SQLite | **Accepted 2026-06-05** |

**ADR-010 — Transactional schema design** is the next planned ADR (account / transaction / lot / valuation / payee / category / rule / import_batch).

Full index and summaries: [`docs/adr/README.md`](docs/adr/README.md).

---

## Known pitfalls

Most legacy-specific pitfalls only matter while maintaining the v0.1 web app; the cross-cutting ones carry into the rewrite.

**Carry forward:**
1. **Windows date formatting** — `%-d` doesn't work. Use `f"{d.day} {d.strftime('%b %Y')}"`.
2. **Full file replacements lose manually-added code** — verify imports survive after any full Write. The `import hashlib` / `compute_hash` pair was lost more than once in v0.1; don't repeat the mistake on the rewrite.
3. **IRI namespace discipline** — Transactions are MFL namespace, accounts/person are MRL. Carrying this into SQLite as stored strings means the mistake travels silently if you generate the wrong prefix on insert.

**Legacy-only:**
4. **pyoxigraph ASK returns bool** — `bool(store.query("ASK {...}"))`.
5. **`@app.on_event` deprecated in FastAPI** — use lifespan context manager.
6. **Starlette 1.0 TemplateResponse** — `request` is first positional arg, not in context dict.
7. **mfl-ontology.ttl must not be empty** — was created as 0-byte placeholder originally.

---

## Development workflow

### Run the desktop app (the live target)
```powershell
# from the project root, with an activated venv
pip install -r mfl_desktop\requirements.txt

# one-time: create the database and seed a first account
python -m mfl_desktop.cli init

# optional: add more accounts
python -m mfl_desktop.cli add-account "Joint Savings" --type savings
python -m mfl_desktop.cli add-account "Amex Gold"     --type credit

# launch the GUI
python -m mfl_desktop
```

### CLI smoke tests
```powershell
python -m mfl_desktop.cli import path\to\statement.qfx
python -m mfl_desktop.cli list --limit 30
python -m mfl_desktop.cli categories
```

### Run the v0.1 web app (legacy, still works on Oxigraph)
```powershell
python main.py
# open http://127.0.0.1:8000
```

### Run the original PySide6 prototype (reference only)
```powershell
cd prototype_register
.\.venv\Scripts\Activate.ps1
python register_proto.py
```

### Starting a new session
1. Read this file.
2. Skim [`docs/adr/README.md`](docs/adr/README.md) for decision context.
3. Check memory under `~/.claude/projects/.../memory/` for owner preferences and project state.
4. Confirm where work is in flight before editing.
5. Deliver complete files; verify imports survive.
6. New architectural decisions need a new ADR (even if owner says "your call").
