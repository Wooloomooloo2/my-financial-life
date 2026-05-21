# CLAUDE_CONTEXT.md
# My Financial Life — Developer Context for AI Assistance

This file gives a new Claude session full context to continue development
without needing the original conversation transcript.

Last updated: May 2026 (end of initial development session)

---

## Project overview

**My Financial Life (MFL)** is a locally-run personal finance application.
Single-user, all data stays on the user's machine. Sister app to
**My Retirement Life (MRL)**, eventually sharing the same database.

**Owner:** Not a developer — prefers complete file replacements over
code snippets. Always provide complete, ready-to-save files.

**Stack:** Python 3.13 + FastAPI + pyoxigraph 0.5.8 + Jinja2 + HTMX +
Tailwind/DaisyUI + Chart.js. No JavaScript frameworks.

**Run:** `python main.py` from the project root. Opens at
`http://127.0.0.1:8000`. Data persists in the Oxigraph store between runs.

---

## Repository structure

```
C:\Projects\my-financial-life\
├── main.py                          # App entrypoint, lifespan, router registration
├── requirements.txt
├── CLAUDE_CONTEXT.md                # This file
├── docs/
│   ├── adr/                         # ADR-001 through ADR-007
│   └── ontology/
│       ├── mrl-ontology.ttl         # v1.0.1 — shared with MRL, do not edit
│       └── mfl-ontology.ttl         # v0.1.0 — MFL-specific, edit carefully
└── app/
    ├── api/
    │   ├── accounts.py              # Account CRUD, register, delete account
    │   ├── dashboard.py             # Dashboard route (GET / and GET /dashboard)
    │   ├── import_routes.py         # Full import workflow
    │   ├── settings.py              # Profile/settings
    │   └── transactions.py          # Inline edit, bulk update, delete transaction
    ├── core/
    │   ├── accounts/
    │   │   ├── accounts.py          # Account data layer, delete_account
    │   │   └── person.py            # Person/profile
    │   ├── dashboard/
    │   │   └── dashboard.py         # Dashboard data layer
    │   ├── import_engine/
    │   │   ├── csv_parser.py        # CSV format detection, CsvColumnMapping
    │   │   ├── import_service.py    # Staging, classification, commit
    │   │   └── ofx_parser.py        # OFX/QFX via ofxtools
    │   ├── ontology/
    │   │   ├── iri_factory.py       # IRI generators for all instance types
    │   │   └── namespaces.py        # All namespace constants (MRL, MFL, MFLX etc.)
    │   ├── transactions/
    │   │   └── transactions.py      # Transaction data layer
    │   ├── template_globals.py      # Jinja2 globals (setup_state, user_initials)
    │   └── templates.py             # Jinja2 environment
    ├── data/
    │   ├── ontology_loader.py       # Loads TTL files into Oxigraph named graphs
    │   └── store.py                 # Singleton Oxigraph Store instance
    └── templates/
        ├── base.html                # Layout, sidebar nav, setup progress banner
        ├── accounts/
        │   ├── _type_fields.html    # HTMX partial for account type fields
        │   ├── list.html            # Accounts list grouped by family
        │   └── new.html             # Add account form
        ├── dashboard/
        │   └── index.html           # Dashboard with chart and summary cards
        ├── import/
        │   ├── map.html             # Column mapping for generic CSV
        │   ├── preview.html         # Import preview before confirming
        │   ├── result.html          # Post-import summary
        │   └── upload.html          # File upload form
        ├── settings/
        │   └── profile.html         # Profile/settings form
        └── transactions/
            ├── _row.html            # Transaction row partial (includes delete button)
            └── register.html        # Full register with pagination, bulk bar, modals
```

---

## Namespaces

```python
MRL  = "https://myretirementlife.app/ontology#"
MRLX = "https://myretirementlife.app/ontology/ext#"
MFL  = "https://myfinanciallife.app/ontology#"
MFLX = "https://myfinanciallife.app/ontology/ext#"

DATA_GRAPH     = NamedNode("https://myfinanciallife.app/data/graph")
ONTOLOGY_GRAPH = NamedNode("https://myfinanciallife.app/ontology/graph")
```

---

## IRI patterns (ADR-006)

- **Accounts and Person:** `mrl:ClassName_N` (integer, MRL-compatible)
  - e.g. `mrl:CashAccount_1`, `mrl:Person_1`
  - Use `iri_from_key("CashAccount_1")` → `NamedNode(MRL + "CashAccount_1")`

- **Transactions, ImportBatches, ValuationEvents:** `mfl:ClassName_<uuid8>`
  - e.g. `mfl:Transaction_a3f7c901`
  - Use `mfl_iri_from_key("Transaction_a3f7c901")` → `NamedNode(MFL + "...")`
  - **CRITICAL:** Never use `iri_from_key` for transactions — they live in MFL namespace not MRL.

---

## Key data layer patterns

### Querying

```python
# SPARQL SELECT
for row in store.query(sparql_string):
    value = row["variableName"].value   # .value gives Python string

# SPARQL ASK — returns bool directly
exists = bool(store.query("ASK { ... }"))

# Quad scan — faster for simple lookups
for quad in store.quads_for_pattern(subject, predicate, None, DATA_GRAPH):
    obj = quad.object.value
```

### Writing

```python
store.update("""
    INSERT DATA {
        GRAPH <https://myfinanciallife.app/data/graph> {
            <subject_iri> <predicate_iri> "value"^^<xsd:type> .
        }
    }
""")
```

### Starlette 1.0 TemplateResponse (CRITICAL)

```python
# Correct — request is FIRST positional arg, not in context dict
return templates.TemplateResponse(request, "template.html", {"key": "value"})
```

---

## Account types and families

| Key | Label | Family | Liability | Balance from |
|---|---|---|---|---|
| `cash_std` | Current account | cash | No | Transactions SUM |
| `savings_std` | Savings account | cash | No | Transactions SUM |
| `credit_std` | Credit card | credit | Yes | Transactions SUM |
| `investment_std` | Investment account | investment | No | Latest valuation |
| `property_std` | Property | property | No | Latest valuation |

---

## Transaction statuses

| IRI | Label | Usage |
|---|---|---|
| `mflx:TransactionStatus_Pending` | Pending | Manual entry, not yet on statement |
| `mflx:TransactionStatus_Uncleared` | Uncleared | Imported, needs review |
| `mflx:TransactionStatus_Cleared` | Cleared | Verified correct |
| `mflx:TransactionStatus_Reconciled` | Reconciled | Matched to closing balance |

Import default: first import → Cleared (historical load); subsequent → Uncleared (review).
Banktivity CSV imports always honour per-transaction status from file.

---

## Import workflow

```
Upload (OFX/QFX/CSV)
    ↓
parse_and_stage() → returns (token, "preview"|"map")
    ↓ if "map"
/import/map/{token}        ← column mapping for unknown CSV
    ↓ POST with CsvColumnMapping
apply_mapping_and_stage()
    ↓ if "preview" (or after mapping)
/import/preview/{token}    ← shows new/duplicate/match classification
    ↓ POST with import_status + accepted_match fitids
commit_import()
    ↓
/import/result             ← summary with link to register
```

**Duplicate detection:**
- OFX: uses bank FITID as import hash (stored as `mfl:importHash`)
- CSV: uses MD5(account_iri + "|" + date + "|" + amount + "|" + payee_raw)[:12]

**Potential match:** manual entry on same account, same amount, same type, date ±2 days.
Default: accept (merge). Merge adds import hash to manual entry, preserves user data.

---

## Pagination

`get_transactions_for_account(account_detail, page=1, per_page=50)` returns
`(rows: list[TransactionRow], total: int)`.

Running balances are computed for ALL transactions then sliced — so page 3
shows correct balances that include pages 1 and 2.

Register route accepts `?page=N&per_page=N` query params.

---

## CSV format detection

`_detect_format(lines)` returns `"banktivity"`, `"creditcard"`, or `"generic"`.

- **Banktivity:** Row 1 = account name (≤2 commas), row 2 has Type/Status/Date/Payee headers.
  Per-transaction status honoured. Amounts have £ symbol and commas. Date format M/D/YY.
  Split transactions collapsed to parent total. Transfers imported as debit.
- **Credit card:** Headers contain `debitCreditCode` or `merchant.name`.
  Date is ISO 8601 with time component. Amount always positive, direction from debitCreditCode.
- **Generic:** Falls through to column mapping UI at `/import/map/{token}`.

---

## Category taxonomy

Stored in ontology graph as SKOS concept scheme.

```
mflx:TransactionCategoryScheme
├── mflx:TransactionCategory_Income
│   ├── Benefits / state payments
│   ├── Freelance / self-employment
│   ├── Investment income
│   ├── Other income
│   ├── Rental income
│   └── Salary
├── mflx:TransactionCategory_Expense
│   ├── Charity and gifts
│   ├── Childcare
│   ├── Dining out
│   ├── Education
│   ├── Groceries
│   ├── Healthcare
│   ├── Holidays and travel
│   ├── Housing
│   ├── Insurance
│   ├── Other expense
│   ├── Savings and investments
│   ├── Shopping
│   ├── Subscriptions
│   ├── Transport
│   └── Utilities
└── mflx:TransactionCategory_Uncategorised
```

---

## Completed features (v0.1)

- ✅ Profile setup (name + base currency → mrl:Person_1)
- ✅ Account management — all 5 types, opening balance, delete account
- ✅ Transaction register — inline editing (category, status, payee, memo)
- ✅ Bulk editing — select rows, set category/status/payee/memo
- ✅ Manual transaction entry
- ✅ Pagination — configurable 25/50/100/250 per page
- ✅ Delete individual transactions
- ✅ OFX/QFX import — duplicate detection, smart status default, match with manual entries
- ✅ CSV import — Banktivity, credit card, generic column mapper
- ✅ Dashboard — net worth, income, expenditure, net cashflow, spending chart, recent transactions
- ✅ Dashboard timescale — MTD, Last Month, YTD, 3M, 6M, 12M

---

## Remaining MVP item

**Register search, filter and sort** — the last v0.1 item.

Requirements:
- Text search across payee and memo (case-insensitive substring)
- Filter by status (All / Uncleared / Cleared / Reconciled)
- Filter by category (All / Uncategorised / specific category)
- Column sort — date, amount, payee, category (asc/desc)
- All combinable — e.g. Uncleared + Uncategorised = post-import review view
- Bulk select still works on filtered results
- Filters and sort state preserved in URL query params for bookmarking

---

## Post-MVP backlog (v0.2+)

**High priority:**
- Category and payee rules engine — payee pattern → auto-assign category on import
- QIF import — for Quicken/Banktivity migrators
- Manual transaction entry: add memo + category fields at entry time (not post-edit)

**Medium priority:**
- User-defined categories with hierarchy — add categories from Banktivity CSV on import
- Transfer categories — "Transfer to [Account]" as a category type, prevents double-counting
- Double-entry transfer import — post to both accounts, with matching logic
- Reconciliation workflow

**v1.0 scope:**
- Budget planning
- Reports (spending trends, income vs expenditure history)
- My Retirement Life database integration
- Export / backup
- Multi-currency transfer handling

---

## Known pitfalls

1. **`mfl_iri_from_key` vs `iri_from_key`** — Transactions use MFL namespace,
   accounts/person use MRL. Using the wrong one causes silent failures.

2. **pyoxigraph store.load() with to_graph silently drops triples** — always use
   the temp-store copy approach in `ontology_loader.py`.

3. **pyoxigraph ASK queries return bool** — `bool(store.query("ASK {...}"))`.
   SELECT queries return an iterable.

4. **Windows date formatting** — `%-d` format code doesn't work on Windows.
   Use `f"{d.day} {d.strftime('%b %Y')}"` instead.

5. **Starlette 1.0 TemplateResponse** — `request` is first arg, not in context dict.

6. **Full file replacements lose manually-added code** — whenever providing a
   complete file, check the current version first using the transcript or the
   file in the repo. Commonly lost: `import hashlib`, `compute_hash` function.

7. **mfl-ontology.ttl must not be empty** — file was created as 0-byte placeholder
   initially. Always verify with `/ontology/parse-mfl` debug endpoint.

8. **`@app.on_event` deprecated** — use `@asynccontextmanager async def lifespan(app)`.

---

## Debug endpoints (in main.py)

```
GET /ontology/status         → triple count in ontology graph
GET /ontology/reload         → force reload both TTL files
GET /ontology/categories-debug → SPARQL category query dump
GET /ontology/scan           → raw quad scan for skos:inScheme triples
GET /ontology/parse-mfl      → isolated parse test of mfl-ontology.ttl
```

---

## requirements.txt

```
fastapi==0.136.1
uvicorn[standard]==0.46.0
pyoxigraph==0.5.8
Jinja2==3.1.6
python-multipart==0.0.27
python-dotenv==1.2.2
platformdirs==4.9.6
pydantic==2.13.4
rdflib==7.6.0
starlette==1.0.0
ofxtools==0.9.4
```

---

## How to continue in a new chat

1. Share this file with Claude at the start of the session.
2. State which feature you want to build or fix.
3. Ask Claude to read specific source files before making changes.
4. Always ask for complete file replacements, not code snippets.
5. If an error occurs, paste the full terminal traceback.
6. After any full file replacement, check the imports section matches the
   previous version (regressions most commonly lose manually-added imports).
