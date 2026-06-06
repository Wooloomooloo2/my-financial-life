# CLAUDE_CONTEXT.md
# My Financial Life — Developer Context for AI Assistance

This file gives a new Claude session full context to continue development
without needing the original conversation transcript.

**Last updated:** 2026-06-06 — session shipped visual baseline + paintEvent charts (ADR-026), Create Schedule From Transaction (ADR-027), Payee aliases round 1 (ADR-029 + ADR-012 amendment).

---

## Status at a glance

- **v0.1 shipped** as a local web app (FastAPI + HTMX + Oxigraph). MVP complete, owner-only. Now in maintenance mode.
- **2026-06-05 pivot:** MFL is being rebuilt as a **native desktop application** (PySide6 + SQLite) for Windows-first distribution. See [ADR-008](docs/adr/ADR-008-desktop-ui-framework.md), [ADR-009](docs/adr/ADR-009-storage-engine-for-ledger-data.md), and [ADR-010](docs/adr/ADR-010-transactional-schema-design.md).
- **Desktop app under `mfl_desktop/` is the live target.** Multi-account register with an All-transactions cross-account view, OFX/QFX/CSV import working end-to-end through the Qt UI, layered architecture (UI → proxy → model → Repository → SQLite). Owner has loaded six months of real data into it (~1,300 transactions) and confirmed the feel.
- **Basic management round complete (2026-06-05):** new + delete + bulk-edit transaction (Ctrl+E modal with per-field checkboxes); account CRUD (create / edit / delete) with opening balance; payee + category management dialogs with rename / merge / delete (cross-merge/-kind-merge rejected explicitly); category `kind` (income/expense/transfer) with cascade on reparent and direct Change Kind verb; Banktivity-style account folders in the sidebar with balance roll-up; File → Save Copy As… / Open… for `.mfl` snapshots; register search now covers payee/memo/amount/date and is comma-insensitive; category combos in dialogs are searchable typeaheads.
- **Reports round 1 (2026-06-05):** Reports → Spending Over Time (stacked bar by top-level expense category group, granularity weekly/monthly/quarterly/annually, date range, account/category/Uncategorised filters, average line, strict-outflow semantics per ADR-018); Reports → Net Worth (Pocketsmith-style three-column layout, big total + horizontal proportional bar + colour-coded legend / Assets / Debts, grouped by account type with per-account drill-down, +Asset / +Debt buttons opening the existing AccountDialog).
- **Transfers (2026-06-05):** category-driven (ADR-020). No dedicated New Transfer verb — picking a `kind='transfer'` category on any flow (New Transaction, inline cell edit, Bulk Edit) prompts for the destination account and creates a partner row sharing one `transfer_id`. Direction inferred from source amount sign. Delete is partner-aware. Migration 0004.
- **Generic CSV mapping wizard (2026-06-05):** unknown-format CSVs (Pocketsmith, etc.) now open a `CsvMappingDialog` — file-preview at top, mapping form in the middle, live after-mapping preview at the bottom (ADR-021). Smart defaults pre-fill all five fields from the existing alias lists; user just confirms for conventional layouts. No schema change. Known formats still commit silently per the no-dialog-for-known-imports rule. Saved mapping profiles (auto-skip the dialog for repeat imports) are explicitly deferred to a future ADR. Shipped alongside a fix to `_classify_and_stage` so within-batch composite-hash collisions (two CSV rows with the same date/amount/empty-payee) get a deterministic `:N` suffix instead of blowing up the `UNIQUE(account_id, import_hash)` constraint at commit.
- **Register typeahead delegates (2026-06-05):** Payee and Category inline editors are now repository-backed typeaheads matching the dialog flows (ADR-022). `PayeeTypeaheadDelegate` wraps `QLineEdit` + `QCompleter` over `Repository.list_payee_names()`; `CategoryTypeaheadDelegate` reuses the same `make_category_picker` helper as the dialog combos and reads `list_categories_flat()` fresh per editor open (no delegate rebind needed). Typing an unknown category name in the Category cell pops a single Yes/No confirm; on Yes the category is created top-level with `kind='expense'`, `source='user'` (aligned with the import path's default) and committed into the cell. Lightweight `_reload_category_cache()` on the window refreshes the cached choice list + filter combo without resetting the model, so the inline-create path is safe to call from inside a delegate `setModelData`.
- **Scheduled transactions / bills (2026-06-05):** First half of the budget arc (ADR-023). New `scheduled_txn` table (migration 0005) stores templates: cadence (weekly/biweekly/monthly/quarterly/annual), anchor date, next-due date, optional end date, signed estimated amount, `variable` flag (prompt at post), `auto_post` flag. Posting materialises one `txn` (or a transfer pair via `create_transfer` when category kind = transfer) and advances next-due via anchor-based math (Jan 31 monthly → Feb 28 → Mar 31). Manual post by default; launch-time `auto_post_due(today)` sweep catches up auto-posters loop-style (a user gone two months gets every missed occurrence). New dialogs: `ScheduleDialog` (single-record edit, transfer destination revealed when category kind = transfer) and `SchedulesDialog` (Manage → Schedules… — table view + New/Edit/Post Now/Delete). No txn → schedule back-link in v1.
- **Budget core (2026-06-05):** Second half of the budget arc (ADR-024). New `budget` / `budget_account` (M:N perimeter) / `budget_category` (target + cadence + role) tables (migration 0006). One budget per file in v1; schema supports many. Perimeter rule: transfers between two in-perimeter accounts cancel out; transfers crossing the perimeter count. Per-category amount stored positive; direction inferred from `category.kind`. Role on `budget_category` (bills / saving / discretionary). Actuals bucket against the **nearest budgeted ancestor** of each txn's category; un-budgeted-chain txns fall into "Other". Pure-Python `mfl_desktop/budget_calc.py` does pro-rating (`365.25/12` etc., identity for matching cadence on its calendar period) and the four Simplifi tiles (Income after bills & saving / Planned spending / Other spending / Available). **Transfer-kind budget categories are skipped in the tile math** (post-ship amendment to ADR-024). Cash-on-hand badge in the header is the separate reality-check. New screens: `BudgetSetupDialog` (two-tab modal — Accounts + Categories) and `BudgetWindow` (non-modal `QMainWindow` under a new top-level **Budget** menu, Ctrl+B, refreshes on `WindowActivate`). No rollover.
- **Budget visualisations (2026-06-06):** Round C of the budget arc (ADR-025). Closes the four ADR-024 deferrals. **Full-cadence-period subtitle** on non-monthly cards (e.g. £1,800 annual Holidays card now shows "£147.84/mo" + a second line "£1,800 annually · this year: £450 of £1,800"). **Burn-down chart** under the tile strip — QtCharts line chart with Actual cumulative outflow vs. linear Ideal pacing + vertical today marker. **Proportional summary bar** (reusing `ProportionalBar` from ADR-019) showing Bills / Saving / Planned / Other / Available proportions with colour-keyed legend — owner's no-pies rule (ADR-018) preserved. **Scheduled-but-not-posted projection** per card: "+£X expected" badge surfacing un-posted scheduled outflows whose `next_due_date` falls inside the screen period. All computation in `mfl_desktop/budget_calc.py` as optional kwargs to `compute_budget_view`; pure-function contract preserved. New widget `mfl_desktop/ui/burn_down_chart.py`. Cadence-period containment uses the global anchor rule (Monday weeks, calendar quarters/years; bi-weekly = 14-day window ending today pending per-schedule anchors).
- **Budget arc state (2026-06-06):** All three rounds shipped end-to-end, but the screen is **rough** — owner flagged it as needing further polish iterations after real use. Likely targets for the next pass: card-layout density (current vertical stack of header + cash badge + 4 tiles + bar + chart + cards eats ~480px before any card is visible), ~~visual tone of the QtCharts default styling on the burn-down~~ (closed 2026-06-06 by ADR-026 — burn-down is now a hand-rolled paintEvent widget matching the spending chart), labelling clarity on the tile colours, and the "Other" bucket behaviour when the user hasn't set up budget categories yet. Don't treat any specific bit of the screen as final — every part is fair game when polish work resumes.
- **Visual baseline + paintEvent charts (2026-06-06):** ADR-026 settled the "1990's Microsoft" complaint. Fusion style + a custom Tailwind v3 slate / `blue-600` QPalette + a minimal QSS layer are applied once from `__main__` via `mfl_desktop/ui/theme.py::apply_theme(app)`. Both charts (Spending Over Time, budget burn-down) hand-rolled as `paintEvent` `QWidget`s after a side-by-side comparison vs. QtCharts and pyqtgraph — owner picked paintEvent for the modern flat look (rounded top corners, soft gridlines, pill-shaped dashed average label, hover tooltip) plus full typography control plus zero new dependency. Shared chart primitives in `mfl_desktop/ui/chart_helpers.py` (palette, `nice_ticks`, `fmt_currency`, `legend_chip`); spending and burn-down both consume them. pyqtgraph removed from requirements; QtCharts no longer imported by either chart widget. Memory: future chart shapes default to paintEvent ([[feedback-chart-engine-preference]]).
- **Create Schedule From Transaction (2026-06-06):** ADR-027 adds a right-click verb on the register that seeds the `ScheduleDialog` from a transaction (account / payee / category / amount / memo / transfer-destination if applicable). The dialog field renamed to **"Next occurrence:"** in both seeded and from-scratch flows. The handler steps `txn.posted_date` forward by the default cadence (`monthly`) until the result is strictly after today, passes that as `seed.anchor_date`; on save anchor = next_due = that future date. New frozen `ScheduleSeed` dataclass + `seed=` kwarg on `ScheduleDialog`; new `Repository.get_transfer_partner_account_id`. No schema change.
- **Payee aliases round 1 (2026-06-06):** First round of the three-round payee arc planned in ADR-028. Migration 0007 adds `payee.canonical_id` (self-ref, `ON DELETE SET NULL`) + partial index. `Repository` gains `set_alias_of` / `promote_to_canonical` / `list_canonical_payees` / `list_aliases_of`; `list_payee_names` filtered to canonicals only (typeahead + bulk-edit completer now suggest preferred labels only); `list_payees_with_usage` rewritten with rolled-up counts on canonicals; `merge_payees` patched to re-point sources' aliases onto the target before deletion. `PayeeRow` extended (`canonical_id`, `canonical_name`, `direct_usage_count`). Payees dialog gets 3 columns (Name / Alias of / Used in), indented aliases, and two new verbs (**Make &Alias of…**, **&Promote to Canonical**). `ADR-012` amended with the canonical/alias model and the three-shape verb table (Merge / Alias / Delete). **Round 2** (import-time alias lookup) and **round 3** (rules engine) deferred per ADR-028 plan. Round 1 deliberately doesn't roll up the register's per-row display to the canonical — round 2 sets `txn.payee_id` to the canonical at import time, which avoids a silent rewrite-on-no-change pitfall in the inline typeahead.
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
│   │   ├── 0001_initial.sql         # ADR-010 schema + seeded categories
│   │   ├── 0002_category_kind.sql   # ADR-014: kind column + Transfer seed
│   │   ├── 0003_account_folders.sql # ADR-015: account_folder + account.folder_id
│   │   ├── 0004_transfers.sql       # ADR-020: txn.transfer_id + partial index
│   │   ├── 0005_scheduled_txn.sql   # ADR-023: scheduled_txn template + due-date indexes
│   │   ├── 0006_budgets.sql         # ADR-024: budget + budget_account + budget_category
│   │   └── 0007_payee_canonical.sql # ADR-029: payee.canonical_id + partial index
│   ├── import_engine/               # Lifted from app/core/import_engine/
│   │   ├── ofx_parser.py            # OFX/QFX — verbatim from v0.1
│   │   ├── csv_parser.py            # Banktivity / credit-card / generic CSV (syntax bug fixed)
│   │   └── import_service.py        # Stage + classify + commit, rewritten against Repository
│   └── ui/
│       ├── register_window.py       # QMainWindow with sidebar + register
│       ├── register_model.py        # QAbstractTableModel — single-account + all-transactions modes
│       ├── filter_proxy.py          # Sort / filter on underlying values
│       ├── delegates.py             # Payee + Category typeahead delegates + Status combo (ADR-022)
│       ├── category_picker.py       # Shared editable-combo helper for category fields
│       ├── csv_mapping_dialog.py    # ADR-021: column-mapping wizard for unknown CSV formats
│       ├── schedule_dialog.py       # ADR-023: single-record schedule create/edit
│       ├── schedules_dialog.py     # ADR-023: schedule list + CRUD + Post Now
│       ├── budget_setup_dialog.py   # ADR-024: perimeter + per-category setup
│       ├── budget_window.py         # ADR-024/025: budget screen (tiles + bar + chart + cards)
│       ├── burn_down_chart.py       # ADR-025: cumulative outflow vs ideal pacing
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

### Register UX

Items (1)–(3) of the original cluster shipped under ADR-022 (typeahead delegates + inline category create). Item (4) bulk-edit shipped in the basic-management round under ADR-017. Follow-ups noted in ADR-022's consequences section:

- **Kind-aware inline create.** Today's confirm dialog fixes `kind='expense'` for the new category. If real use shows the owner regularly wanting income or transfer-kind categories created inline, the right v2 is a tiny kind radio inside the confirm dialog — not a separate full-create dialog path.
- **Memo history typeahead.** The memo cell is still a bare `QLineEdit`. A repository-backed completer over distinct memo strings would slot in symmetrically with the payee delegate.

### Polish backlog from 2026-06-05 basic-management round

- **Visible "New Transaction" button.** Today the only entry points are the Transaction menu and Ctrl+N. A toolbar / register-pane button would be more discoverable. Owner asked for this during step 1; deferred to a later UI polish pass.
- **Unlock kind combo on New sub-category.** In the New Category dialog, when a parent is chosen the kind combo is locked to the parent's kind. For mixed-kind structures (e.g. Paycheck with Gross Pay = income and Taxes = expense beneath it), creating a different-kind child is currently two steps (create as inherited kind, then Change Kind). Editing this to leave the kind combo *unlocked* (just defaulting to parent's kind) is the obvious fix when real-world use confirms the need.
- ~~**Spending Over Time chart visuals.**~~ Closed by ADR-026 (paintEvent rewrite — modern flat look, rounded top corners, soft gridlines, hover tooltips, pill-shaped average label). Surviving sub-items (cleaner axis labels like `Jan 2026`, optional numbers-on-bars toggle, Save Chart As Image) carried into the new "Reports — round 2 follow-ups" section below.

### Budget arc — follow-ups after round C (2026-06-06)

The three-round arc (ADR-023 / 024 / 025) is complete. Items deliberately left for later polish or to feed real-use feedback:

- **Per-card sparkline** of historical actuals over the last N periods — a thin chart inside each card showing the trend, scoped to the card's own cadence period. Deferred from round C to keep card-layout simple while owner lives with the screen.
- **Reports → Budget vs Actual** time-series window — a separate report showing planned vs actual per category over a date range. Bigger window of its own; defer until use surfaces a clear need beyond the current per-month view.
- **Overdue-schedules surface on the budget window** — round C deliberately omits overdue schedules (next_due_date before period start) from the per-card "expected" badge. A small "X overdue schedules — review in Manage ▸ Schedules" indicator on the budget header would close the visibility gap.
- **Per-schedule (and per-budget_category) cadence anchor** — the global Monday-weeks / calendar-quarters rule from ADR-023 holds for both schedules and budget cards. A user whose paycheck is a Friday-paid biweekly schedule would benefit from a per-row anchor override; needs UI in the schedule and budget setup dialogs and a small schema migration to add the anchor column on budget_category (it's already on scheduled_txn).
- **Rollover** of unspent surplus / deficit between periods — a per-`budget_category` rollover flag. Additive when needed.
- **Multi-budget per file** — schema already supports it; UI surfaces a single default budget today.
- **Stepped burn-down ideal** that drops at known scheduled-txn dates rather than a single straight line. Adds nuance without new data.
- **Budget palette extraction** — round C inlines the segment / chip colours in `budget_window.py` and the chart series colours in `burn_down_chart.py`; consolidate if a wider palette unification ever lands.

### Payee arc — round 1 shipped (2026-06-06)

Three-round arc planned in **ADR-028** (Proposed). **Round 1 shipped 2026-06-06** as **ADR-029** (Accepted) + an amendment to **ADR-012**.

- **Round 1 (shipped)**: migration 0007 adds `payee.canonical_id` + partial index. `Repository` gains `set_alias_of`, `promote_to_canonical`, `list_canonical_payees`, `list_aliases_of`; `list_payees_with_usage` rewritten with canonical rollup; `list_payee_names` filtered to canonicals only (typeahead + bulk-edit completer); `merge_payees` re-points sources' aliases onto the target before deletion. `PayeeRow` extended with `canonical_id` / `canonical_name` / `direct_usage_count`. PayeesDialog gets 3 columns (Name / Alias of / Used in), indented aliases, and two new verbs (Make Alias of…, Promote to Canonical). Sort-on-column-0 disabled so canonical→aliases grouping survives. **Deferred deliberately**: register inline display doesn't roll up to canonical (round 2's import-engine work is the natural place — silent rewrite-on-no-change pitfall via the inline typeahead if done at display layer).
- **Round 2** (own arc, after Reports round 2): import-time alias lookup. Exact match at minimum, pattern / fuzzy as follow-ups inside the arc.
- **Round 3** (own arc, after round 2): rules engine — the long-deferred `rule` table finally wired up. Matchers + setters + priority + Manage → Rules dialog.

**Decisions still ahead** (per-round ADRs to settle): round-2 match strategies; round-3 rule priority semantics + retroactive application; (lower priority) per-row register display rollup; (lower priority) a "Show aliases" toggle in the Payees dialog.

**Round 3 scope sharpening (added 2026-06-06)** — owner sketched the round-3 UI: a dedicated screen that shows **both** aliases (the round-1 manual mappings) and rules side-by-side, with create/delete affordances. Rule creation: matcher type (`contains` / `starts-with` / `ends-with` / `is-exactly`) + string + target canonical payee. Aliases are essentially the `is-exactly` case already, so round 3 should consider unifying the two — either by treating the round-1 `payee.canonical_id` rows as an implicit `is-exactly` rule in the unified view, or by migrating them into a single `payee_rule` (or similar) table with a `match_kind` column. Decision deferred to round-3 planning ADR; this note exists so the round-3 author starts with the unified-screen intent in hand rather than discovering it mid-build. Owner's wording: *"a dedicated screen to be able to see all of the aliases, and be able to create or delete rules. You'd be allowed to say 'if contains xxxx string, then xxxx payee' or 'starts with, ends with, is exactly' etc."*

### Reports — round 2 follow-ups (2026-06-06)

Owner saw the potential of the paintEvent approach (ADR-026) and flagged that reports has a lot more work to do — what shipped under ADR-018 is the floor, not the ceiling. Quoted reactions on the paintEvent landing: "Resizing is cleaner, the default hover over feels a bit more natural." Concrete items raised:

- **Default to top-level category rollup on Spending Over Time.** Today the chart rolls up to the *second* tier (Banktivity-style "group" — `Expense → Groceries → Tesco` rolls to `Groceries`), per `reports.py::category_group_map`. Owner wants the default to be the *top* level — everything under `Expense` rolls to `Expense`. Existing group-tier behaviour should stay available, presumably via a new "Rollup" control on the panel (Top level / Group / Leaf). Implementation surface: a new helper alongside `category_group_map` (e.g. `category_root_map`) plus a rollup control in `spending_report_window.py`. SQL aggregation already returns per-`category_id` so the Python rollup is the only thing that changes.
- **Hierarchical category pickers (charts, budget, transactions).** Searching for a top-level category name should reveal its descendants in the results, even when the descendant text doesn't match. Typing "Food" should show `Food`, `Food → Dining out`, `Food → Groceries`, `Food → Bars and Restaurants`. Affects: spending-report category checklist, budget setup category combos, register Category typeahead delegate (ADR-022), every category combo built via `make_category_picker`. Likely shape: a `QSortFilterProxyModel` whose `filterAcceptsRow` returns true when any ancestor's name matches the search text, with a tree-view popup variant for the picker (instead of the flat `QCompleter` ADR-022 ships). Needs its own ADR — sub-decisions on whether the result list flattens with breadcrumbs (`Food → Groceries`) or genuinely renders as a tree, and whether the same control is reused for filter-checklists (where multi-select matters) and value-pickers (where single-select matters).
- **Broader reports arc.** v0.1 had a dashboard and several views that haven't been ported yet. Owner wants to come back to reports as a thread now that the rendering ceiling is clear — likely a multi-round arc like the budget one. Open questions for the arc planning: which reports to ship, in what order, against which user task (cash flow, income vs spending, category trends, account balance history, net-worth trajectory). Treat as an open thread, not a single ADR.

### Other deferred items

- **Saved CSV mapping profiles.** Follow-up to ADR-021: persist the mapping the user just used (keyed by a normalised header signature) so the next Pocketsmith (or other unknown-format) import skips the wizard and commits silently. Needs its own ADR — header-signature scheme, conflict handling when export columns are renamed, profile-management UI (edit/rename/delete).
- **QIF parser.** v0.2 high-priority; lift the QIF format alongside the existing OFX/CSV parsers.
- ~~**Categorisation rules engine.**~~ Absorbed into round 3 of the payee-aliases-and-rules arc — see ADR-028 (planning) and the "Payee arc" section below.
- **Per-lot IRR / ROI.** Schema is in place (`lot`, `valuation`); no computation yet.
- **Category management UI.** Re-parent, rename, archive categories — needed to manage what import-created and to undo path-conflict separations after the fact.
- **Dashboard.** v0.1 had it; needs porting to Qt — charts per ADR-026 (paintEvent), reusing `mfl_desktop/ui/chart_helpers.py`.
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
| ADR-010 | Transactional schema design | **Accepted 2026-06-05** |
| ADR-011 | Account delete policy — hard delete now, archive reserved | **Accepted 2026-06-05** |
| ADR-012 | Payee name-management policy | **Accepted 2026-06-05** |
| ADR-013 | Category management policy | **Accepted 2026-06-05** |
| ADR-014 | Category kind (income/expense/transfer) | **Accepted 2026-06-05** |
| ADR-015 | Account folders in the sidebar | **Accepted 2026-06-05** |
| ADR-016 | File save / open model — auto-commit + Save Copy As snapshots | **Accepted 2026-06-05** |
| ADR-017 | Bulk edit shape — modal dialog with per-field checkboxes | **Accepted 2026-06-05** |
| ADR-018 | Reports framework + first chart — Spending Over Time | **Accepted 2026-06-05** |
| ADR-019 | Net Worth report — three-column Assets / Net Worth / Debts | **Accepted 2026-06-05** |
| ADR-020 | Account transfers — category-driven, two linked txns sharing one transfer_id | **Accepted 2026-06-05** |
| ADR-021 | Generic CSV column-mapping wizard | **Accepted 2026-06-05** |
| ADR-022 | Register typeahead delegates + inline category create | **Accepted 2026-06-05** |
| ADR-023 | Scheduled transactions (bills, recurring income, recurring transfers) | **Accepted 2026-06-05** |
| ADR-024 | Budget core — perimeter + per-category targets + screen | **Accepted 2026-06-05** |
| ADR-025 | Budget visualisations — burn-down + summary bar + cadence subtitles + scheduled projection | **Accepted 2026-06-06** |
| ADR-026 | Visual style baseline (Fusion + custom palette) and chart-engine paintEvent | **Accepted 2026-06-06** |
| ADR-027 | Create Schedule From Transaction — right-click verb on the register | **Accepted 2026-06-06** |
| ADR-028 | Payee aliases, canonical labels, and the auto-categorisation arc (planning) | **Proposed 2026-06-06** |
| ADR-029 | Payee aliases round 1 — data model + manual alias UI | **Accepted 2026-06-06** |
| ADR-012 (amend) | Canonical / alias model + Merge/Alias/Delete verb table | **Accepted 2026-06-06** |

Full index and summaries: [`docs/adr/README.md`](docs/adr/README.md).

---

## Known pitfalls

Most legacy-specific pitfalls only matter while maintaining the v0.1 web app; the cross-cutting ones carry into the rewrite.

**Carry forward:**
1. **Windows date formatting** — `%-d` doesn't work. Use `f"{d.day} {d.strftime('%b %Y')}"`.
2. **Full file replacements lose manually-added code** — verify imports survive after any full Write. The `import hashlib` / `compute_hash` pair was lost more than once in v0.1; don't repeat the mistake on the rewrite.
3. **IRI namespace discipline** — Transactions are MFL namespace, accounts/person are MRL. Carrying this into SQLite as stored strings means the mistake travels silently if you generate the wrong prefix on insert.
4. **CSV import hashes can collide within one batch.** The composite `date|amount|payee_raw` hash isn't unique across a single file — two coffees on the same day at the same price, or any rows with an unmapped payee column, will collide. `_classify_and_stage` resolves this with a deterministic `:N` suffix; any future rewrite of the staging path must preserve this or the `UNIQUE(account_id, import_hash)` constraint will fire at commit.

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
