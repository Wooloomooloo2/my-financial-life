# ADR-082 — Date & period standardisation: one period vocabulary, one date-edit factory

**Date:** 2026-06-16
**Status:** Accepted (period-vocabulary consolidation shipped + verified; the date-*format* factory rollout across the remaining `QDateEdit` sites is the documented follow-up — "P1 part B").
**Amends:** ADR-033 amendment (account-summary period presets), ADR-041 (register date-window presets), ADR-056/ADR-046/ADR-064/ADR-066/ADR-068 (the report windows' inline period handling).
**Related:** `docs/RELEASE_1.0_BACKLOG.md` workstream **P1** (decision **P1a** — one user-facing date format = `d MMM yyyy`), the Qt-free compute-layer convention (ADR-009-era — `account_summary`/`fx`/`holdings` carry no Qt).

---

## Context

A 1.0-polish audit found the date/period handling fragmented across the app:

- **Three+ period-preset vocabularies**, each defined separately: register (`30d/90d/6m/12m/ytd/all`), reports (`quarter/6m/ytd/1y/3y/custom`), investment returns (`ytd/1y/3y/5y/max/custom`), Sankey (`ytd/mtd/last_month/custom`).
- **Period-bounds maths duplicated** in four places: `account_summary.period_bounds`, the register's `_current_since` + `_months_before`, and inline `_resolve_bounds` methods in the Sankey and Investment-Returns windows.
- **The label dictionary copy-pasted into ~10 files** (`_PERIOD_LABELS` in eight report windows/dialogs + two investment ones).
- **A latent inconsistency:** the register computed `6m`/`12m` **calendar-accurately** (`_months_before` — same day-of-month N months back, the documented-correct behaviour), while `account_summary.period_bounds` used **day-deltas** (`180`/`365` days). So `"6m"` silently meant two slightly different windows depending on which screen you were on.
- **Two date display formats** across ~14 `QDateEdit` sites — ISO `yyyy-MM-dd` on data-entry forms, `d MMM yyyy` on a couple of human dialogs, and one (`schedule_dialog`) with **no calendar popup** at all.

The preset **keys are persisted** in saved-report `filters_json`, so consolidation must keep them byte-stable.

### Options considered
1. **One Qt-free period module + one Qt date-edit factory; keep the per-context preset *sets*; delegate every consumer (CHOSEN).**
2. **One single preset set everywhere.** Rejected — the sets are legitimately different (a register wants `All`; investments want `5y`/`max`; a cash-flow Sankey wants `mtd`/`last_month`). Forcing one menu would worsen the UX and break persisted keys.
3. **Leave the duplication, just sync the values by hand.** Rejected — it's exactly the drift (the `6m` discrepancy) that caused the bug.

---

## Decision

### `mfl_desktop/periods.py` — the single source of truth (Qt-free)
A new compute-layer module (no Qt, no SQL — sits beside `account_summary`/`fx`, so dialogs, windows, and the CLI all share it). It owns:
- the **one** `PERIOD_LABELS` registry (every key, one definition);
- the per-context **preset SETS** (`REGISTER_PRESETS`, `REPORT_PRESETS`, `INVESTMENT_PRESETS`, `SANKEY_PRESETS`) — keys unchanged, so saved filters are untouched;
- **one** `period_bounds(key, today, *, earliest=, custom_start=, custom_end=)` resolving every key (returns `Optional[start]` — `None` only for unbounded `all`/`max` with no `earliest`; raises on `custom` without dates);
- `months_before`, `period_since` (the register's ISO lower-bound helper), `fmt_date_range`, `period_label`, `labels_for`, `options_for`.

### `mfl_desktop/ui/date_widgets.py` — the Qt factory
`make_date_edit(...)` (calendar popup on, **`d MMM yyyy`** display per decision P1a, optional today-clamp) and `make_period_combo(keys, current=)` (builds a preset combo from a `periods` set — label as text, key as `itemData`).

### Delegation (consumers now reference the single source)
- `account_summary.py` and `reports/filters.py` **re-export** the vocabulary from `periods` under their historical names (`PERIOD_KEYS`/`PERIOD_LABELS`/`period_bounds`/`fmt_date_range`/`period_display_label`; `SPENDING_PERIOD_KEYS`/`INVESTMENT_RETURNS_PERIOD_KEYS`/`SANKEY_PERIOD_KEYS`), so **every existing importer keeps working unchanged**.
- The register drops its own `_months_before` + preset list + `_current_since` body → `periods.months_before` / `periods.REGISTER_PRESETS` / `periods.period_since`.
- The Sankey and Investment-Returns windows drop their inline `_resolve_bounds` maths → `periods.period_bounds` (custom/`max` edge-handling preserved).
- All ten duplicated `_PERIOD_LABELS` dicts removed: the eight report-shape consumers reuse `account_summary.PERIOD_LABELS`; the two investment ones use the full `periods.PERIOD_LABELS`. (Sankey's inline combo *options list* — which carries a deliberate `Custom…` ellipsis affordance — is left as-is; it's a display list, not the duplicated label dict.)

### Deliberate semantic alignment
Month/year windows are now **calendar-accurate everywhere** (`months_before`), which is what the register always did. The report windows' `6m`/`1y`/`3y` (and investment `5y`) therefore shift from day-deltas to calendar months — a **0–3 day change** to those rolling windows, landing them on the same day-of-month. Day windows (`30d`/`90d`/`quarter`) stay rolling-by-days by design. This removes the `"6m"`-means-two-things bug and is an intentional, minor improvement.

---

## Consequences

### Positive
- **One place** to read or change the period vocabulary, bounds maths, labels, and date-edit construction. The `"6m"` discrepancy is gone.
- **Zero churn for importers** — the re-export aliases mean the ~12 consumer files that referenced the old names still work; persisted filter keys are unchanged (round-trip-verified).
- **`make_date_edit`/`make_period_combo` make the remaining date-format unification a one-liner per site.**
- **Verified:** `periods.py` unit tests (every key's bounds, calendar day-clamp, custom/all/max edges, `period_since`); compute-layer delegation parity; whole-app import; offscreen construction of the spending + investment filter dialogs (period combos build, `5y`/`max` resolve from the shared registry) and the register preset/since logic.

### Negative / trade-offs
- **The report windows' rolling `6m`/`1y`/`3y`/`5y` shift by 0–3 days** (day-delta → calendar). Intended and minor, but it is a behaviour change to existing report default windows.
- **The date-*format* unification is not yet rolled out** — `make_date_edit` exists and is proven, but the ~14 `QDateEdit` sites still set their own formats (incl. the no-popup `schedule_dialog`). Tracked as **P1 part B**: replace each `QDateEdit()` + `setDisplayFormat(...)` with `make_date_edit(...)`, and adopt `make_period_combo` in the filter dialogs to retire the last label references.
- Two new modules to know about (one compute, one UI) — offset by ten-plus files getting simpler.

### Ongoing responsibilities
- New date inputs use `make_date_edit`; new period selectors use `make_period_combo` + a `periods` preset set — **never** a fresh `_PERIOD_LABELS` dict or inline bounds maths.
- A new preset key is added in **one** place (`periods.PERIOD_LABELS` + the relevant set + `period_bounds`); persisted keys are never renamed.
- Complete **P1 part B** (the `make_date_edit` rollout) to fully close the launch-plan P1 "one date-format" item.

---

## Amendment (2026-06-16) — P1a reversed to ISO `yyyy-MM-dd`; Sankey gains Last 6/12 months

**Status:** Accepted.

Two owner corrections after seeing the live app:

1. **Date format → ISO.** The original P1a (`d MMM yyyy`) was an *unconfirmed default* I adopted; the owner actually wants **ISO `yyyy-MM-dd`** (e.g. `2026-01-01`). It surfaced as an inconsistency in the Sankey custom-range flow — the window's range *note* already rendered ISO (`2026-01-01 → …`) while the `CustomPeriodDialog` picker showed `1 Jan 2026`. ISO is also what the app's data-entry fields already used, so this makes the app uniform rather than split. Changes: `make_date_edit`'s default `display_format` → `ISO_DATE_FORMAT`; the four remaining human-format `QDateEdit` sites flipped to ISO (`custom_period_dialog`, `register_filters_popover`, `goal_dialog`, `reconcile_wizard`). The change is display-only — values are read via `QDateEdit.date()`, so persistence is untouched. **Scope decision (owner):** flip the *input fields* only; the prose custom-range **summary labels** (`fmt_date_range` → `Custom: 1 Jun → 6 Jun`) stay as compact prose, since they're a caption, not an editable value. So with the data-entry fields already ISO, **every date input in the app is now `yyyy-MM-dd`** and the P1 "one date-format" item is effectively closed (the remaining P1-part-B work is the `make_date_edit` *adoption* for consistency + the `schedule_dialog` no-popup fix, not a format change).

2. **Sankey timeframe presets.** The Sankey only offered `ytd / mtd / last_month / custom` — missing **Last 12 months**, which is standard in the other reports. `periods.SANKEY_PRESETS` now adds `6m` + `1y` (→ `ytd, mtd, last_month, 6m, 1y, custom`), keeping the cash-flow-native MTD / Last month. While here, the Sankey window's last hand-maintained period list was removed — its combo now builds from `periods.options_for(periods.SANKEY_PRESETS)` (with the `Custom…` ellipsis preserved), so it can't drift from the shared vocabulary again. Existing saved Sankey reports are unaffected (their keys are a subset; selection is `findData`-based).

**Verified** offscreen: the Sankey timeframe combo reads `Year to date / Month to date / Last month / Last 6 months / Last 12 months / Custom…` and `1y` resolves to a 12-calendar-month window; `CustomPeriodDialog` date fields show `yyyy-MM-dd`; `make_date_edit()` defaults to ISO; no `d MMM yyyy` remains in the source; whole-app import clean.
