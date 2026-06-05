# Register prototype — PySide6 + SQLite

A standalone prototype that exists for one purpose: **prove the native register
feels right** before committing to the wider rewrite described in
[ADR-008](../docs/adr/ADR-008-desktop-ui-framework.md) and
[ADR-009](../docs/adr/ADR-009-storage-engine-for-ledger-data.md).

This is throwaway code — it lives outside `app/`, has no connection to the
v0.1 web stack, and will be deleted (or absorbed) once the real rewrite begins.

## What it demonstrates

- **Native data grid** — `QTableView` with a custom `QAbstractTableModel`,
  ~10,000 rows scrolled at native speed with virtualised rendering.
- **Inline edit** — double-click *Payee*, *Category*, *Status*, or *Memo*; the
  edit goes through the repository to SQLite and back to the model. Category
  and Status use combo-box delegates.
- **Sort** — click any column header. The proxy sorts on the underlying value
  (so amounts sort numerically, dates chronologically), not the formatted
  string.
- **Filter** — free-text search across payee/memo, plus *Status* and *Category*
  dropdowns. All three combine.
- **Running balance** — computed at load time in date order so page-jumping
  doesn't lose accuracy. (When you sort by a non-date column the balance
  becomes meaningless — same as in Banktivity.)

## What it does *not* cover

- Lots, valuations, IRR/ROI — out of scope for this surface.
- Multi-account navigation, sidebar, dashboard — register only.
- Import, settings, packaging.
- Connection to MRL or any ontology — this validates the storage and grid
  layers, not the conceptual model.

## Architecture (matches the planned real app)

```
QMainWindow
   │
   ▼
QSortFilterProxyModel        ← filter / sort
   │
   ▼
TransactionTableModel        ← Qt model contract
   │
   ▼
Repository                   ← only file that touches SQL
   │
   ▼
SQLite
```

If the prototype feels right, the same layering carries forward unchanged into
the real app. The only file the rest of the codebase would need to know about
is `Repository`.

## Run

Requires Python 3.13+ on Windows (also works on macOS/Linux).

```powershell
cd prototype_register
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python seed.py            # creates prototype.db with 10,000 transactions
python register_proto.py  # launches the window
```

`seed.py` is deterministic (`random.Random(42)`) so re-running gives the same
dataset. Re-running deletes and rebuilds the database.

## Things to try when evaluating the feel

1. Scroll the full 10k rows with the wheel — should be silky, no flicker.
2. Click a *Category* cell, change the value, watch it persist after restart.
3. Click the *Amount* column header — sort numerically (negatives first).
4. Filter to `Status = Uncleared` and `Category = Uncategorised` simultaneously
   — this is the "post-import review" view, should be near-instant.
5. Type into the search box character by character — filtering should
   re-render without perceptible lag.
6. Compare the row-height and editing keyboard shortcuts to Banktivity.

If any of these feel off, that's the gap to close before the real rewrite
starts.
