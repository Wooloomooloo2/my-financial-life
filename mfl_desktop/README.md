# `mfl_desktop` — the rewrite

The new home for My Financial Life as a native desktop application
(PySide6 + SQLite). See [ADR-008](../docs/adr/ADR-008-desktop-ui-framework.md),
[ADR-009](../docs/adr/ADR-009-storage-engine-for-ledger-data.md), and
[ADR-010](../docs/adr/ADR-010-transactional-schema-design.md) for the
architectural decisions this code implements.

The v0.1 web app under `app/` and `main.py` remains in place but is
**maintenance only**. New work goes here.

## Layout

```
mfl_desktop/
├── db/
│   ├── money.py            — Decimal ↔ INTEGER pence conversion
│   ├── schema.py           — migration runner (manages schema_version)
│   └── repository.py       — Repository class; the only layer that touches SQL
├── migrations/
│   └── 0001_initial.sql    — initial schema per ADR-010 + seed categories
├── import_engine/
│   ├── ofx_parser.py       — lifted verbatim from v0.1
│   ├── csv_parser.py       — lifted with syntax bug fixed and status mapping
│   │                         updated from RDF IRIs to enum strings
│   └── import_service.py   — rewritten against Repository; classification
│                             logic (dup detect, manual match) preserved
└── cli.py                  — smoke-test CLI for the import engine
```

## Smoke test

The CLI is the fastest way to verify the import engine works end-to-end on
real data.

```powershell
# from the project root, with the existing PySide6 venv or any Python 3.13:
pip install ofxtools

# 1. Create the database and seed a Person + one CashAccount
python -m mfl_desktop.cli init

# 2. Import a real file (OFX, QFX, or Banktivity CSV).
#    --status overrides the suggested import status.
#    --accept-matches merges potential matches with manual entries.
python -m mfl_desktop.cli import path\to\statement.qfx --status Cleared

# 3. See what landed
python -m mfl_desktop.cli list --limit 30

# 4. See what categories the import created (Banktivity Parent:Child paths
#    are parsed into the hierarchy by Repository.find_or_create_category_path)
python -m mfl_desktop.cli categories
```

The default database is `mfl_dev.db` in the current directory; pass
`--db PATH` to put it elsewhere.

## What this code does *not* yet do

These are deliberate omissions — they belong to later turns:

- **PySide6 UI.** No windows yet. The CLI exists to validate the import
  engine without waiting for the UI.
- **Generic-CSV column mapping.** The service supports it
  (`apply_mapping_and_stage`) but the CLI does not collect the mapping
  interactively. The Qt UI will.
- **QIF parser.** Listed as v0.2 high-priority backlog; not yet ported.
- **Account / category management commands.** The smoke-test CLI only
  exercises the import path.
- **Categorisation rules engine.** The schema reserves a `rule` table for
  it (per ADR-010) but no service uses it yet.
- **Per-lot IRR / ROI.** Schema is in place (`lot`, `valuation`); no
  computation code yet.

## Notes on the lift

- `app/core/import_engine/csv_parser.py` had a real syntax bug at lines
  120–125 (unindented `if`/`elif` block at module level inside a function
  body). That path had clearly never been exercised in v0.1. Fixed during
  the lift; see the comment in `import_engine/csv_parser.py`.
- The v0.1 Banktivity parser stuffed the source `Category/Account` value
  into the memo string. The lifted version passes it through as
  `category_raw` and the new service parses `Parent:Child` paths into the
  hierarchical category tree via `Repository.find_or_create_category_path`.
- `ImportService` is instance-based, not module-level globals. The v0.1
  pattern (module-level `_pending_imports` dict) only worked because a
  single FastAPI process held the state. A desktop app may hold multiple
  import sessions per window; instance-based is the right shape for it.
