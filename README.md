# My Financial Life

> **Track your money with confidence — private, local, and powerful.**

My Financial Life (MFL) is a free, open-source, locally-run personal finance application for **Windows and macOS** (Linux works too). It gives you a complete, real-time picture of your financial life — transactions, balances, spending trends, budgets, investments, and net worth — without your data ever leaving your machine.

It is a sister application to **My Retirement Life**, and is designed to exchange data so that financial events recorded here — a salary change, a large purchase, a property sale — can feed retirement projections.

---

## Status

🟢 **Native desktop app in active development, feature-rich.** After the 2026-06 pivot from a web app to a native desktop application (PySide6 + SQLite), MFL has grown a deep feature set and is being polished toward a 1.0 release. The owner runs it daily against real data (26 accounts, ~35k transactions, multi-currency).

| Area | Status |
|---|---|
| Accounts — cash / credit / investment / property / vehicle, folders, close & reopen | ✅ |
| Transaction register — inline editing, search, date & amount filters, bulk edit | ✅ |
| Import — OFX / QFX, CSV (Banktivity / credit-card / generic + column-mapping wizard), QIF (investment) | ✅ |
| Transfers — category-driven, matching, bulk reconcile, cross-currency | ✅ |
| Multi-currency — FX rates (openexchangerates), historical backfill, conversion across reports | ✅ |
| Categories & payees — hierarchical tree, kinds, archive/restore; payee aliases, merge | ✅ |
| Auto-categorisation — per-payee remembered category + a pattern rules engine | ✅ |
| Scheduled transactions / bills — cadences, auto-post, due/overdue cues | ✅ |
| Budgeting — envelope/zero-sum hybrid, 12-month matrix, monthly view, burn-down, pay-down & savings goals | ✅ |
| Reports — Spending Over Time, Net Worth, Income & Expense, Payee, Category × Payee, Sankey, Investment Returns; saved reports | ✅ |
| Reconciliation — statement-based wizard | ✅ |
| Investments — holdings (FIFO), prices (Tiingo + manual), market-value net worth, returns/ROI, splits, merges | ✅ |
| Home dashboard — net worth, budget, accounts, bills, recent activity, top payees/categories, investment performance | ✅ |
| Persistence — live auto-saving `.mfl` file, rotating snapshots (GFS retention), Data Library | ✅ |
| Theming / visual polish | 🔧 In progress |
| Automatic bank downloads (direct feeds) | 📋 Planned |
| Packaging — single-file installers (PyInstaller) | 📋 Planned |

---

## Why this exists

Most personal finance tools are cloud-based, subscription-driven, and built for a single country. My Financial Life is built for people with complex financial lives — multiple accounts, multiple currencies, property, investments — who want their data to stay private and on their own machine.

---

## Tech stack

The **live application** is the native desktop app under `mfl_desktop/`:

| Layer | Technology |
|---|---|
| UI | PySide6 (Qt for Python) — hand-rolled `paintEvent` charts (no chart dependency) |
| Storage | SQLite (one `.mfl` file per dataset; WAL mode, auto-commit) |
| Import | `ofxtools` (OFX/QFX); built-in CSV/QIF parsers |
| FX rates | openexchangerates.org (optional, user-supplied key) |
| Security prices | Tiingo (optional, user-supplied key) + manual entry |
| Packaging (planned) | PyInstaller (Windows, macOS) |

Only two third-party runtime dependencies (`PySide6`, `ofxtools`) — SQLite is in the standard library. See [`docs/adr/`](docs/adr/) for the full architecture decision records.

> A legacy v0.1 web app (FastAPI + Oxigraph RDF triple store) still lives at `main.py` + `app/` for reference only — see [Legacy web app](#legacy-web-app-v01).

---

## Running locally

The live application is the native desktop app under `mfl_desktop/` (PySide6 + SQLite).

### Prerequisites
- Python 3.13+
- Git

### Setup

```bash
git clone https://github.com/Wooloomooloo2/my-financial-life.git
cd my-financial-life
python -m venv .venv
```

Activate the virtual environment:

```powershell
# Windows — PowerShell
.\.venv\Scripts\Activate.ps1
```

```bat
:: Windows — cmd.exe
.venv\Scripts\activate.bat
```

```bash
# macOS / Linux
source .venv/bin/activate
```

> **PowerShell note:** use `.\.venv\Scripts\Activate.ps1` (with the `.\` prefix
> and the `.ps1` extension). The bare `.venv\Scripts\activate` form only works
> in cmd.exe. If activation is blocked by execution policy, run
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then retry.

Install dependencies and launch:

```bash
pip install -r mfl_desktop/requirements.txt

# Launch the desktop app
python -m mfl_desktop
```

On first launch the app creates its database in the OS-standard per-user
location (`~/Library/Application Support/MFL/MyFinancialLife.mfl` on macOS,
`%APPDATA%\MFL\…` on Windows, `~/.local/share/MFL/…` on Linux) and seeds a
Person + first account, so no separate setup step is needed (ADR-050 Tier-2 /
ADR-016). The app opens on the **Home dashboard**.

For development, a `mfl_dev.mfl` in the current directory is used when present —
so a checked-out repo launches against its working database with no flag. Pass
`--db PATH` to use a specific file (an explicit path that doesn't exist is *not*
auto-created — seed it with `python -m mfl_desktop.cli init --db PATH`).

### First run
1. Add your accounts (sidebar context menu → New Account, or the Account menu)
2. Import a bank file (OFX / QFX / QIF / CSV) or add transactions manually
3. Set a base currency and any FX / price API keys under **Manage → Currencies…** and **Manage → Securities…**

---

## Import formats supported

| Format | Notes |
|---|---|
| OFX / QFX | FITID-based duplicate detection, status preservation |
| Banktivity CSV | Status (Cleared/Reconciled) preserved; trusts the amount sign over the Type column |
| Credit-card CSV | merchant as payee, debit/credit direction |
| Generic bank CSV | Column-mapping wizard for unknown formats, with live preview |
| QIF (investment) | Securities, buys/sells/dividends, holdings; bank/credit QIF stubbed for a later round |

---

## Project structure

```
my-financial-life/
├── mfl_desktop/                     # the live desktop app (PySide6 + SQLite)
│   ├── __main__.py                  # entry point — opens the register/Home window
│   ├── cli.py                       # init / seed helpers
│   ├── db/
│   │   ├── repository.py            # the only layer that touches SQL
│   │   ├── schema.py                # migration runner
│   │   └── money.py                 # Decimal ↔ integer-pence conversion
│   ├── migrations/                  # NNNN_*.sql forward migrations (0001–0025)
│   ├── import_engine/               # OFX / QFX / CSV / QIF parsers + import service
│   ├── reports/                     # pure report builders (spending, payee, income/expense, …)
│   ├── ui/                          # ~70 PySide6 widgets, dialogs, windows, charts
│   ├── account_summary.py           # pure per-account + bills/Top-N compute
│   ├── budget_calc.py               # pure budget matrix / burn-down compute
│   ├── goal_calc.py                 # pure savings / pay-down goal compute
│   ├── holdings.py                  # pure FIFO holdings, value history, returns
│   ├── rules_engine.py              # pure auto-categorisation matcher
│   ├── home_dashboard.py            # pure Home-screen data assembly
│   ├── fx.py / prices.py            # FX + security-price clients (stdlib urllib)
│   ├── snapshots.py / data_library.py  # backups + dataset management
│   └── account_types.py / currencies.py / transfer_reconcile.py
├── docs/
│   ├── adr/                         # 75 architecture decision records + README index
│   └── ontology/                    # MRL/MFL RDF ontologies (shared with My Retirement Life)
├── main.py + app/                   # legacy v0.1 web app (reference only)
└── prototype_register/              # original PySide6 data-grid prototype (reference)
```

The architecture is layered: **UI → Repository → SQLite**, with the heavy
computation factored into dependency-free, offscreen-testable pure modules
(`budget_calc`, `holdings`, `rules_engine`, `home_dashboard`, `reports/`, …).

---

## Documentation

Architecture and design rationale live as **Architecture Decision Records** in
[`docs/adr/`](docs/adr/) — 75 and counting, each recording the decision, the
alternatives rejected, and the consequences. Start with the index:

- [`docs/adr/README.md`](docs/adr/README.md) — full ADR index with summaries

`CLAUDE_CONTEXT.md` at the repo root is the developer's running context document
(current state, backlog, known pitfalls, ADR table).

---

## Licence

MIT — free to use, modify, and distribute.
