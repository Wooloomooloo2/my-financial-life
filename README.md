# My Financial Life

> **Track your money with confidence — private, local, and powerful.**

My Financial Life is a free, open-source, locally-run personal finance application for Windows, macOS, and Linux. It gives you a complete, real-time picture of your financial life — transactions, balances, spending trends, and net worth — without your data ever leaving your machine.

It is a sister application to **My Retirement Life**, and is designed to eventually share the same database, so that financial events recorded here — a salary change, a large purchase, a property sale — can feed directly into retirement projections.

---

## Status

🟡 **v0.1 — MVP in active development.** Core features working and tested. Register search/filter/sort remaining before v0.1 is complete.

| Feature | Status |
|---|---|
| Account management (all 5 types) | ✅ Complete |
| Transaction register with inline editing | ✅ Complete |
| Pagination (configurable per-page) | ✅ Complete |
| Delete transactions / accounts | ✅ Complete |
| Manual transaction entry | ✅ Complete |
| OFX / QFX import with duplicate detection | ✅ Complete |
| CSV import — Banktivity, credit card, generic | ✅ Complete |
| Column mapper for unknown CSV formats | ✅ Complete |
| Dashboard — net worth, income/expenditure, chart | ✅ Complete |
| Register search, filter and sort | 🔧 In progress |
| Category and payee rules engine | 📋 Post-MVP |
| Reconciliation workflow | 📋 Post-MVP |
| QIF import | 📋 Post-MVP |
| Transfer categories | 📋 Post-MVP |
| User-defined categories | 📋 Post-MVP |
| Budget planning | 📋 v1.0 |
| Reports | 📋 v1.0 |
| My Retirement Life integration | 📋 v1.0 |

---

## Why this exists

Most personal finance tools are cloud-based, subscription-driven, and built for a single country. My Financial Life is built for people with complex financial lives — multiple accounts, multiple currencies, property, investments — who want their data to stay private and on their own machine.

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.13 + FastAPI |
| Data store | Oxigraph (embedded RDF triple store via pyoxigraph 0.5.8) |
| Templating | Jinja2 (server-rendered HTML) |
| Frontend | HTMX + Tailwind CSS + DaisyUI + Chart.js |
| OFX/QFX import | ofxtools |
| Packaging (planned) | PyInstaller (Windows, macOS) + AppImage (Linux) |

See [docs/adr/](docs/adr/) for full architecture decision records.

---

## Ontology

My Financial Life uses a two-layer ontology:

- **`mrl:` namespace** — shared with My Retirement Life. Defines currencies, jurisdictions, persons, and the full account hierarchy. Loaded from `docs/ontology/mrl-ontology.ttl`.
- **`mfl:` namespace** — finance-specific extensions. Defines transactions, payees, category rules, import batches, and valuation events. Lives in `docs/ontology/mfl-ontology.ttl`.

Named graphs:
- `https://myfinanciallife.app/ontology/graph` — ontology triples
- `https://myfinanciallife.app/data/graph` — instance data

---

## Running locally

### Prerequisites
- Python 3.13+
- Git

### Setup

```bash
git clone https://github.com/Wooloomooloo2/my-financial-life.git
cd my-financial-life
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
python main.py
```

Open `http://127.0.0.1:8000` in your browser.

### First run
1. Go to Settings and enter your name and base currency
2. Add your first account under Accounts
3. Import a bank file (OFX or CSV) or add transactions manually

---

## Import formats supported

| Format | Notes |
|---|---|
| OFX / QFX | FITID-based duplicate detection, full status preservation |
| Banktivity CSV | Status (Cleared/Reconciled) preserved, categories stored in memo |
| Credit card CSV | merchant.name as payee, debitCreditCode for direction |
| Generic bank CSV | Column mapping UI for unknown formats |
| QIF | Planned (post-MVP) |

---

## Project structure

```
my-financial-life/
├── main.py                          # FastAPI app, lifespan, router registration
├── requirements.txt
├── docs/
│   ├── adr/                         # Architecture decision records (ADR-001–007)
│   └── ontology/
│       ├── mrl-ontology.ttl         # Shared with My Retirement Life
│       └── mfl-ontology.ttl         # MFL-specific ontology
└── app/
    ├── api/
    │   ├── accounts.py              # Account CRUD + register route
    │   ├── dashboard.py             # Dashboard route
    │   ├── import_routes.py         # Import workflow routes
    │   ├── settings.py              # Profile/settings route
    │   └── transactions.py          # Inline edit, bulk update, delete routes
    ├── core/
    │   ├── accounts/
    │   │   ├── accounts.py          # Account data layer + delete_account
    │   │   └── person.py            # Person/profile data layer
    │   ├── dashboard/
    │   │   └── dashboard.py         # Dashboard data layer
    │   ├── import_engine/
    │   │   ├── csv_parser.py        # CSV format detection + parsing
    │   │   ├── import_service.py    # Classification, staging, commit
    │   │   └── ofx_parser.py        # OFX/QFX parsing via ofxtools
    │   ├── ontology/
    │   │   ├── iri_factory.py       # IRI generation for instances
    │   │   └── namespaces.py        # All namespace constants
    │   ├── transactions/
    │   │   └── transactions.py      # Transaction data layer + delete
    │   ├── template_globals.py      # Jinja2 global functions
    │   └── templates.py             # Jinja2 environment setup
    ├── data/
    │   ├── ontology_loader.py       # Loads TTL files into Oxigraph
    │   └── store.py                 # Singleton Oxigraph store
    └── templates/
        ├── base.html
        ├── accounts/
        ├── dashboard/
        ├── import/
        ├── settings/
        └── transactions/
```

---

## Documentation

- [ADR-001](docs/adr/ADR-001-backend-language-and-triple-store.md) — Python + Oxigraph
- [ADR-002](docs/adr/ADR-002-frontend-stack.md) — HTMX + Tailwind + DaisyUI
- [ADR-003](docs/adr/ADR-003-packaging-strategy.md) — PyInstaller packaging
- [ADR-004](docs/adr/ADR-004-cross-platform-portability.md) — cross-platform approach
- [ADR-005](docs/adr/ADR-005-ontology-strategy.md) — RDF ontology design
- [ADR-006](docs/adr/ADR-006-instance-iri-naming-strategy.md) — IRI naming
- [ADR-007](docs/adr/ADR-007-data-access-patterns.md) — SPARQL data access

---

## Licence

MIT — free to use, modify, and distribute.
