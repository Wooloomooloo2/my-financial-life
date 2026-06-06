# ADR-032 — Vehicle account type and the `vehicle` family

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design — defines `account.type` and its CHECK); ADR-019 (Net Worth report — family-level legend and colour table)

---

## Context

The five account types seeded in ADR-010 (`cash_std`, `savings_std`, `credit_std`, `investment_std`, `property_std`) don't cover vehicles. The owner wants to track car (and any other titled vehicle) values alongside property on the net-worth picture — they depreciate, they're insured, they get sold or replaced, but they're not a building.

Two design questions:

1. **Reuse the `property` family, or add a new `vehicle` family?** The two are structurally similar (asset, mark-to-market in a future revision, balance from valuations once that pipeline exists). But on the Net Worth screen they get one row each per family in the legend and the proportional bar. Folding vehicles under "Property" would mislabel a car as a building; folding "Vehicles" under "Property" in the legend would force a sub-row that the family-based view doesn't support.
2. **Schema migration shape.** SQLite can't `ALTER` a CHECK constraint in place. Adding a sixth allowed `type` value means recreating the `account` table — the standard 6-step swap (recreate with new constraint, copy, drop old, rename, recreate indexes lost with the original table, leave FK references to be auto-resolved by name).

## Options considered

### Family

- **Reuse `property`** — a car is an asset; the net-worth column already groups assets by family. Code-wise this is one line in `account_types.py`.
  - Rejected. The visual identity in the Net Worth legend would lump cars and houses into "Property". The owner explicitly framed this as a separate type, not a sub-classification of property.
- **New `vehicle` family** (chosen) — `account.family = 'vehicle'`, label `Vehicles`, a fresh colour on the legend. Slots in alongside `investment / property / cash / credit` in `_FAMILY_VIEW`. The Net Worth screen already iterates over `_FAMILY_VIEW` and silently skips families with no accounts, so adding a row is additive — no other code change required to surface the new family when the user creates their first vehicle.

### Colour for the new family

The legend palette today is `blue-600 / teal-500 / green-500 / pink-500` (investment / property / cash / credit). The new colour should sit between property and cash in the asset progression and stay readable against the slate background. Picked `amber-500` (`#f59e0b`) — adjacent to property's teal in hue distance, distinct from every existing chip, and matches the Tailwind v3 ramp the rest of the palette is anchored to (ADR-026).

### Migration shape

- **In-place ALTER on the CHECK** — SQLite doesn't support it. Rejected by physics.
- **Drop the CHECK entirely** — the Python source-of-truth (`account_types.py`) already validates type values, so the DB-level CHECK is partly redundant. Tempting because it's a smaller migration.
  - Rejected. The CHECK has caught at least one bug already (typoed type strings in CLI flows) and serves as defense-in-depth against future code paths that might bypass the Python validation. Worth the one-time migration cost.
- **Table-recreate with widened CHECK** (chosen) — standard SQLite procedure: `PRAGMA foreign_keys=OFF`, create `account_new` with the new CHECK, copy rows, drop old, rename new, recreate the `idx_account_folder` index that follows the table, `PRAGMA foreign_keys=ON`. FK references INTO `account` from `txn`, `lot`, `valuation`, `import_batch`, `scheduled_txn`, `budget_account`, etc. are stored by table name and auto-resolve to the renamed table once the swap completes.

## Decision

### `mfl_desktop/account_types.py`

Add a sixth `AccountTypeSpec`:

```python
AccountTypeSpec("vehicle", "vehicle_std", "Vehicle", "VehicleAccount", "vehicle", False)
```

- CLI key `vehicle`, storage `vehicle_std`, label `Vehicle`, MRL class `VehicleAccount` (ADR-006 convention), family `vehicle`, not a liability. The class name follows the existing `<Type>Account` convention (CashAccount, SavingsAccount, …) so future MRL integration treats vehicles symmetrically with the other asset types.

### `mfl_desktop/migrations/0008_vehicle_account_type.sql`

Recreates `account` with the widened CHECK:

```sql
CHECK (type IN (
    'cash_std','savings_std','credit_std','investment_std',
    'property_std','vehicle_std'
))
```

Re-creates `idx_account_folder` after rename (dropped with the original table). FK references INTO `account` survive the rename automatically.

### `mfl_desktop/ui/net_worth_window.py`

Add `("vehicle", "Vehicles", QColor("#f59e0b"), "asset")` to `_FAMILY_VIEW` between Property and Cash & Bank. No other code change — the iteration already silently skips families with no accounts, and the type-level rollup in `_compute_type_totals` walks `ACCOUNT_TYPES` so the `Vehicle` row appears under Assets once the user adds their first vehicle.

### `docs/schema.sql`

Reference-schema copy updated alongside the migration (family comment + `type` comment + CHECK).

## Consequences

### Positive

- Owner can track vehicles as a distinct row on the Net Worth screen, side-by-side with property and cash.
- Symmetric with property at the data layer: balance comes from `opening_balance + sum(txn.amount)` today; when the deferred `valuation` mark-to-market pipeline lands (already in the schema since ADR-010), it lights up for vehicles for free.
- Adding the type is one row in `account_types.py` + one row in `_FAMILY_VIEW`. Every code path that walks `ACCOUNT_TYPES` (AccountDialog, sidebar grouping, type-level Net Worth rollup, CLI add-account) inherits the new type with no further change.

### Negative / trade-offs

- Six allowed values where previously there were five. Next account-type request (e.g. business / pension / crypto) is one more migration; the table-recreate pattern is now precedented so the next time is cheap.
- The fresh amber colour adds a fourth asset hue to the Net Worth legend. The proportional bar widens its colour vocabulary to four distinct asset segments — still readable, still distinct, but the palette is getting fuller. A future revision might consolidate into pattern-fills or labelled segments if the legend ever feels crowded.

### Ongoing responsibilities

- Any future account type follows the same shape: ADR + one-row migration that recreates `account` with a widened CHECK + one `AccountTypeSpec` + one `_FAMILY_VIEW` row.
- The MRL `VehicleAccount` class name is reserved in the `mrl:` namespace pattern by usage here; if MRL ever ships a vehicle-related concept (registration, insurance policy, depreciation schedule) the names should align.
- The `valuation` table from ADR-010 will eventually need a "vehicle" code path (KBB / Autotrader-style valuation source rather than property indices). Out of scope for this ADR; tracked under the broader valuation-pipeline backlog.
