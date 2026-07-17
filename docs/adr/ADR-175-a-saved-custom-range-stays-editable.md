# ADR-175 — A saved custom date range stays editable

**Date:** 2026-07-17
**Status:** Implemented
**Related:** ADR-056 / ADR-146 (the Sankey report and its filter dialog). ADR-082 (the shared period vocabulary + `CustomPeriodDialog`). ADR-039 (saved reports). ADR-084 (the report filter dialog base, which — deliberately — the Sankey filter dialog adopts *without* a period block).

## Context

Owner-reported: *"when you create a report with a custom date range, afterwards it's not possible to edit that date range."*

It is specific to the **Sankey (Cash Flow)** report, and the reason is a signal choice. Every report edits its timeframe in one of three ways:

- **Inside the Filter dialog** (Spending, Income, Income & Expense, Investment) — the From/To pickers are always live when the preset is Custom. Verified working: a saved custom report reopens with the pickers enabled and populated.
- **Checkable toolbar buttons** (Account Summary, the register drill) — `QPushButton.clicked` fires on *every* press, including of the already-checked button, so re-clicking "Custom" always re-opens the editor.
- **A toolbar `QComboBox`** — only Sankey. Its "Custom…" item opens `CustomPeriodDialog`, and the combo was wired to **`currentIndexChanged`**.

`currentIndexChanged`, by design, does not emit when the selection does not change. A saved custom report loads with the combo already showing "Custom…" (`_sync_controls_from_filters` sets it). So the one gesture the user needs — pick "Custom…" to re-edit the range — is a no-change selection, and the signal never fires. The range is frozen. Confirmed by driving the real window: with the combo on custom, `setCurrentIndex(custom)` opened no dialog.

## Decision

**Wire the period combo to `activated`, and always open the editor on a custom pick.** Three parts:

**1. `activated` instead of `currentIndexChanged`.** `QComboBox.activated` fires on every user selection from the popup — the current item included — which is exactly "the user chose Custom, re-open the editor". Crucially it does **not** fire on a programmatic `setCurrentIndex`, so `_sync_controls_from_filters` (which seeds the combo when a report loads) stays silent and cannot trigger a spurious dialog. This is the canonical Qt signal for "act on the click, not on the change".

**2. Seed the editor from the *stored* range.** When re-editing an existing custom range, `CustomPeriodDialog` opens on the dates being edited (`custom_start`/`custom_end`), not on the preset bounds `_resolve_bounds` computes. Editing 1 Feb–31 May should open on 1 Feb–31 May, not on this quarter.

**3. Guard the no-op re-pick.** `activated` fires for re-selecting the current *preset* too (where `currentIndexChanged` stayed silent), so the handler returns early when a non-custom key equals the current one — otherwise clicking the already-active timeframe would mark a clean report dirty and re-render for a selection that changed nothing. Custom is deliberately exempt from this guard: re-picking custom must always open the editor, which is the whole fix.

## Rejected

- **`currentTextActivated` / handling the click on the combo's line edit.** `activated(int)` is the documented signal for this and matches the existing `*_a` handler signature; nothing is gained by a text- or event-level alternative.
- **Adding a separate "Edit range…" button next to the combo.** A second control for a state the combo already represents. The combo *is* the timeframe picker; the fix is making it behave like one.
- **Moving Sankey's period into its Filter dialog** to match the other reports (which would inherit the always-live pickers and sidestep the combo entirely). The larger, more consistent change — and out of proportion to a one-line signal fix. Worth considering if the Sankey toolbar is ever reworked; noted, not done.
- **Switching the depth/value combos to `activated` too, for consistency.** They don't open dialogs, so re-selecting the current value being a silent no-op is correct for them. Only the period combo has the "re-pick must re-fire" requirement; changing the others would gain nothing and would add the same no-op-dirty risk the guard above exists to prevent.

## Consequences

- **A saved Sankey report's custom range is editable again** — pick "Custom…" and the editor opens on the stored dates, whatever the combo already shows.
- **No behaviour change for the other reports**, which never had the bug (their period lives in the Filter dialog or on checkable buttons). This is a Sankey-only, one-signal fix.
- Re-picking the current preset is now an explicit early-return rather than an implicitly-suppressed signal. Same user-visible result (nothing happens), but the reason is in the code instead of in a Qt signal's emission rule.
- The other two `CustomPeriodDialog` callers (Account Summary, register drill) were checked and are correct by construction — checkable buttons, not a combo. No change needed, and the ADR records that they were looked at so the next reader doesn't have to re-derive it.

`tests/test_sankey_custom_period_editable.py` 6/6, driving the real window offscreen with a stubbed `CustomPeriodDialog`: re-picking Custom while already on custom **opens the editor** (the bug); the editor is **seeded from the saved range**, not preset bounds; the edited range is applied; cancelling keeps the old range; re-picking the same preset **does not dirty** the report; and switching to a different preset still works. The first two were confirmed to **fail against the old `currentIndexChanged` wiring**.

Full suite 418 passed, 0 failed. No schema change.
