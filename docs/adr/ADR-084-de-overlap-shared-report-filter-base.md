# ADR-084 — Remove overlapping functionality (launch P3): shared report-filter base, shared transfer-match presentation, affordance audit

**Date:** 2026-06-18
**Status:** Accepted (P3a — `ReportFilterDialogBase` — is the first slice; P3b/P3c follow within the arc).
**Amends:** ADR-039 (saved-report filter dialogs), ADR-046/056/064/066/068 (the per-report filter dialogs), ADR-036/037 (the two transfer-match UIs).
**Related:** `docs/RELEASE_1.0_BACKLOG.md` workstream **P3** ("Remove overlapping functionality"); builds on ADR-082's `periods.py` / `date_widgets.py` single-source vocabulary.

---

## Context

The 1.0 launch plan's **P3** asks us to remove *overlapping* functionality. A code audit (2026-06-18) confirmed the four candidates the backlog flagged, but they are **not the same kind of overlap** — and the distinction is the whole point of this ADR:

> **Not all overlapping functionality is redundant.** Two *different workflows* that happen to reach a similar outcome are legitimate, complementary affordances — pruning them is hostile, not tidy. Only **divergent duplicates of the *same* thing** (copy-pasted code that can drift, or two presentations of one concept that should look identical) are genuine redundancy to consolidate.

Applying that lens to the four audited candidates:

1. **Six report filter dialogs are ~40% copy-paste** — *genuine redundancy*. `spending` / `income_expense` / `payee` / `category_payee` / `investment_returns` / `sankey` each re-declare the same `_set_combo_to`, `_initial_custom_dates`, `_sync_custom_visibility`, the period-combo + custom-date triad, the `_GRANULARITY_OPTIONS` constant, the accounts `CheckListPanel` + all-checked→`[]` normalisation, and the button-box/accept scaffold. Their *specials* genuinely differ (rollup, securities, top-N, transfers, category tree). **→ consolidate the shared 40%, keep the specials.**

2. **"Schedules has 4 entry points"** — *legitimate affordances, NOT redundancy*. Menu (Manage ▸ Schedules), register filter-bar button, and the Home "Bills due" card all open `SchedulesDialog` via the one `_on_manage_schedules` handler — but they are three distinct **discovery paths**, and the toolbar button uniquely carries the overdue/due-soon badge (ADR-063). The right-click "Create Schedule From Transaction" (ADR-027) opens a *different* dialog (`ScheduleDialog`, seeded from the txn). **None is a dead duplicate. → audit + document, no amputation.**

3. **Two transfer-match UIs** (inline confirm/picker ADR-036 vs Manage ▸ Reconcile Transfers ADR-037) — *partial redundancy*. The strength chip (`_CHIP_COLOURS`, the pill widget) and `_fmt_amount` are copy-pasted into both files; the candidate-**row** layouts genuinely differ (single-card vs picker-table vs two-sided pair-table). **→ extract the shared presentation primitives, keep the bespoke row layouts.**

4. **`TransactionsListWindow` reachable only via drill** — *intentional, not overlap*. Navigation is breadcrumb-chip removal + period swap (no back-stack); ADR-083 already verified every drill target opens the one shared window. **→ confirm + document, no change.**

---

## Decision

P3 ships as one arc in three slices. The unifying rule throughout: **consolidate duplicated *implementation*; never remove a distinct *affordance*.**

### P3a — `ReportFilterDialogBase` (the big rock; this slice)

A **toolkit base class** `mfl_desktop/ui/report_filter_dialog_base.py` (`ReportFilterDialogBase(QDialog)`), not a rigid slot-filling layout. It offers opt-in builder methods + shared helpers; each subclass still assembles its own layout and constructs its own result object. This kills the copy-paste without forcing one layout that would fight Sankey's category tree, Investment's securities panel, or Spending's rollup rebuild.

**The base owns (shared 40%):**
- `GRANULARITY_OPTIONS` — the one `(label, value)` list (was re-declared in two files).
- `_make_period_combo(keys, current)` — builds + stores `self._period_combo`, wires `currentIndexChanged → _sync_custom_visibility`.
- `_make_custom_dates(period_key, custom_start, custom_end)` — builds `self._custom_from/_to` via the unified `_initial_custom_dates` (with the `period_bounds` fallback the investment variant was missing).
- `_make_granularity_combo` / `_make_transfers_check` / `_make_top_n_spin` / `_make_accounts_panel` — the recurring specials shared by ≥2 dialogs.
- `_sync_custom_visibility()` — enable/disable the custom-date edits (guarded so period-less dialogs can inherit).
- `_period_and_custom()` — reads the combo + date edits → `(period_key, custom_start, custom_end)` with the silent from>to swap.
- `_checked_or_all(panel)` — the all-checked→`[]` normalisation, one place.
- `_finalise(body)` — adds the `QDialogButtonBox`, wires `accepted → self._on_accept` / `rejected → reject`.
- `values()` — returns `self._result`.
- `_set_combo_to` / `_initial_custom_dates` — the duplicated statics, now inherited.

**Each subclass keeps:** its constructor signature (unchanged — windows call them exactly as before), its unique widgets and layout, and its `_on_accept` building the type-specific dataclass (or, for Sankey, the `(accounts, categories)` tuple). The persisted `filters_json` keys are untouched, so saved reports round-trip identically.

**Sankey adopts partially** — it has no period block and returns a tuple, but still inherits `_make_accounts_panel`, `_checked_or_all`, `_finalise`, and `values()`. Its category **tree** (`CategoryTreePanel`) stays bespoke.

### P3b — shared transfer-match presentation

Extract the duplicated primitives into a shared module (`mfl_desktop/ui/transfer_chips.py`): the `STRENGTH_COLOURS` map, a `strength_chip(...)` widget, `fmt_amount(...)`, and the cross-currency `rate_deviation_tooltip(...)` string builder. Both `transfer_match_dialogs.py` and `transfer_reconcile_dialog.py` consume them so the two surfaces look identical. The strength **bins** already live in pure `transfer_reconcile.strength_for_score` — the colour map joins them as the presentation sibling. Row layouts stay per-dialog.

### P3c — affordance audit (documentation outcome)

The schedules entry points and the `TransactionsListWindow` navigation are **confirmed intentional and left in place**; this ADR is the record that they were audited under the affordance-vs-redundancy rule and deliberately kept. The only permissible change here is a small consistency tweak if one is found — none was required.

---

## Consequences

### Positive
- **One place** for the report-filter plumbing: a new report adds only its specials; the period/accounts/granularity/normalisation boilerplate is inherited. Removes the drift surface (e.g. the investment dialog's subtly-different `_initial_custom_dates`).
- **Two transfer-match surfaces can't visually diverge** — the chip and amount formatting are shared.
- **The "schedules" question is settled on record** — future tidying won't re-litigate deleting a working affordance.
- It is also the natural home for the P4 dialog-polish pass (sizing, button order) — one base to style, not six.

### Negative / trade-offs
- A toolkit base is looser than a rigid slot-filling base: subclasses still write their own layout assembly. Deliberate — the specials are too divergent for one fixed layout, and a rigid base would force awkward escape hatches for Sankey/Investment.
- Six dialog files + two transfer dialogs are touched; each is re-verified headless/offscreen (no behaviour change intended; `filters_json` round-trip is the regression guard).

### Ongoing responsibilities
- New report filter dialogs subclass `ReportFilterDialogBase` and use its builders — never a fresh `_set_combo_to` / `_initial_custom_dates` / granularity list.
- New transfer-match surfaces use `transfer_chips` — never a fresh `_CHIP_COLOURS`.
- The affordance-vs-redundancy rule (consolidate implementation, keep distinct workflows) is the standing test for any future "de-overlap" work.
