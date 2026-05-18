# My Financial Life

> **Track your money with confidence — private, local, and powerful.**

My Financial Life is a free, open-source, locally-run personal finance application for Windows, macOS, and Linux. It gives you a complete, real-time picture of your financial life — transactions, balances, spending trends, and net worth — without your data ever leaving your machine.

It is a sister application to **[My Retirement Life](https://github.com/Wooloomooloo2/my-retirement-life)**, and is designed from the ground up to eventually share the same database, so that financial events recorded here — a salary change, a large purchase, a property sale — can feed directly into your retirement projections.

---

## Why this exists

Most personal finance tools are cloud-based, subscription-driven, and built for a single country. My Financial Life is built for people with complex financial lives — multiple accounts, multiple currencies, property, investments — who want their data to stay private and on their own machine.

---

## What it does

- **Account management** — current accounts, savings, credit cards, investments, and property in any currency
- **Transaction tracking** — import via CSV or OFX/QFX, or enter manually
- **Smart categorisation** — auto-categorisation rules built from your import history
- **Net worth** — live calculation across all accounts, assets and liabilities
- **Dashboard** — spending trends, category breakdown, income vs expenditure, configurable timescale
- **Privacy first** — all data stored locally in an embedded database; no accounts, no cloud sync, no telemetry

---

## Sister app — My Retirement Life

My Financial Life shares its ontology and data model with **My Retirement Life**, a retirement planning and projection application. The two apps are designed to eventually share a single database, so that:

- A salary change recorded in My Financial Life updates retirement projections automatically
- A property purchase or sale flows through to net worth and retirement planning
- Investment account balances tracked here feed the retirement projection engine

Both apps use the same tech stack, the same RDF data store, and the same core ontology. See [ADR-005](docs/adr/ADR-005-ontology-strategy.md) for the full design decision.

---

## Who it's for

- Anyone who wants a clear, honest picture of their day-to-day finances
- People with complex financial lives — multiple countries, currencies, or account types
- My Retirement Life users who want their financial data to feed their retirement projections
- Anyone who wants their financial data to stay private and on their own machine

---

## Status

🟡 **Early development** — project structure and ontology complete, account and transaction screens in progress. Not yet ready for general use.

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.13 + FastAPI |
| Data store | Oxigraph (RDF triple store, embedded via pyoxigraph) |
| Templating | Jinja2 (server-rendered HTML) |
| Frontend | HTMX + Tailwind CSS + DaisyUI + Chart.js |
| Packaging | PyInstaller (Windows, macOS) + AppImage (Linux) |
| OFX import | ofxtools |

See [docs/adr/](docs/adr/) for the full architecture decision records.

---

## Ontology

My Financial Life uses a two-layer ontology:

- **`mrl:` namespace** — shared with My Retirement Life. Defines currencies, jurisdictions, persons, and the full account hierarchy. Source of truth lives in the My Retirement Life repository.
- **`mfl:` namespace** — finance-specific extensions. Defines transactions, payees, category rules, import batches, and valuation events.

Both TTL files live in [docs/ontology/](docs/ontology/).

---

## Running locally (development)

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

The app will start and open in your default browser at `http://127.0.0.1:8000`.

---

## Documentation

- [Architecture Decision Records](docs/adr/) — why the stack was chosen
- [Ontology](docs/ontology/) — data model design

---

## Contributing

Contributions are welcome. Please open an issue before submitting a pull request so we can discuss the approach first.

---

## Licence

MIT — free to use, modify, and distribute.
