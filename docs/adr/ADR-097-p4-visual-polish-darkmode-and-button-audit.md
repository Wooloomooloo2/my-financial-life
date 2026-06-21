# ADR-097 — P4 visual polish: dark-mode completion + dialog-button consistency audit

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-076 (theming / design tokens — the three earlier dark-mode rounds), ADR-084 (`ReportFilterDialogBase`), ADR-026 (paintEvent chart palette), `RELEASE_1.0_BACKLOG.md` item **P4**.

---

## Context

P4 ("visual & interaction polish for a paying audience") bundles several loosely-related items. This ADR records the first concrete pass and the audit conclusions for the rest, so a later session knows what was *decided not to change* vs. what is still open.

**Dark-mode completion.** ADR-076 round 3 fixed the budget matrix's *model brushes* — hardcoded `QColor`s returned from `data()` that neither the palette nor the `themed()` registry could reach, so they stayed light under the dark palette. A sweep for the same bug class (item/widget colours set from frozen light hex) across the whole UI turned up four stragglers on **table/tree item foregrounds** and inline label stylesheets, all on surfaces the dark-mode rounds hadn't revisited:

- `net_worth_window` — the "(no rate)" account flag (`QColor("#b45309")`).
- `investment_returns_window` — the returns table's gain/loss cells + the Performers list % labels, all off module-level `_GAIN`/`_LOSS` constants frozen at import.
- `statements_window` — the status column (In progress / Out of balance / Reconciled) + the summary line, off module-level `_MUTED`/`_GREEN`/`_AMBER`/`_RED`.
- `transfer_match_dialogs` — the picker's "Create new partner" sentinel row (`QColor("#2563EB")`).

The reference fix already in the codebase is `account_summary_window`'s holdings table: resolve `tokens.c(...)` **at populate time**. Every one of these windows rebuilds its table/tree on activate-refresh, so a populate-time resolve is dark-mode-correct on the next refresh — matching the accepted pattern.

**Dialog-button consistency.** The backlog also lists "consistent button order (platform-native), default-button + Esc across all dialogs." An audit of all ~47 `QDialog` subclasses found this is **already substantially satisfied**: ~40 use `QDialogButtonBox`, which gives native macOS-vs-Windows button ordering, automatic Esc→reject, and role-based default buttons for free. The hand-rolled button rows that remain fall into two legitimate groups — accept/reject dialogs that already set a default and wire reject (`TransferMatchConfirmDialog`, `ReconcileWizard`), and *action-toolbar* management dialogs (`PayeesDialog`, `SecuritiesDialog`, `SchedulesDialog`, `RulesDialog`, `CategoriesDialog`) whose buttons are verbs (New / Edit / Delete) beside a Close, not an OK/Cancel pair.

---

## Decision

**(1) Finish the dark-mode item-colour sweep.** The four stragglers now resolve their colours from theme tokens:

- `net_worth_window` → `tokens.c("warning")`.
- `investment_returns_window` → `_colour()` returns `tokens.c("positive")` / `tokens.c("negative")` (resolved live, feeding both the table foregrounds and the inline stylesheet strings); the Performers % label uses the same tokens; the frozen `_GAIN`/`_LOSS` constants are deleted.
- `statements_window` → `_status_text` returns a token *name* (`"warning"` / `"negative"` / `"positive"`), resolved at the call site; the summary label uses `tokens.themed(…, "color: {muted_strong};")`; the frozen colour constants are deleted.
- `transfer_match_dialogs` → `tokens.c("accent")`.

Light values are unchanged by construction (the tokens' light hexes equal the old constants), with one one-shade exception: the statements "In progress" amber moves from `#D97706` (amber-600) to the `warning` token's `#b45309` (amber-700) — there is no token whose light value is exactly the old amber-600, and `warning` is the closest semantic + visual match (it is also exactly the colour `net_worth`'s "no rate" flag already used). Negligible in light mode; correct in dark.

**(2) No button-row changes — the audit conclusion is "already consistent."** `QDialogButtonBox` is and stays the standard for accept/reject dialogs (it is what delivers platform-native ordering). The action-toolbar dialogs keep their hand-rolled verb rows: forcing a default button onto a toolbar whose primary action is **Delete** would make Enter destructive, so *not* setting a default there is correct, not a gap. This is recorded so a future pass doesn't "fix" a non-bug.

---

## Alternatives considered

- **Route chart *series* colours through tokens too.** Rejected — the established pattern (`balance_flow_chart`, explicitly commented "local to this chart, not the GROUP_PALETTE") is that data-series brand colours are fixed literals that read on both light and dark *surfaces*; only the structural colours (surface/grid/axis/ink, via `chart_helpers`) theme-switch. The newer charts (`burn_down_chart`, `loan_schedule_view`) already follow this, so they were left as-is.
- **Migrate every dialog to `QDialogButtonBox`.** Rejected — the remaining hand-rolled rows are action toolbars, not OK/Cancel pairs; `QDialogButtonBox` is the wrong abstraction for a row of verbs.
- **Add a new amber-600 token to preserve the statements colour pixel-for-pixel.** Rejected as over-engineering for one status word; reused `warning`.

---

## Consequences

- Dark mode is now complete across the audited item-colour surfaces; a theme toggle followed by the normal activate-refresh shows correct colours everywhere these windows render.
- The button-consistency item is closed as "no change required," with the reasoning captured so it isn't re-opened.
- **Still open under P4** (recorded, deliberately not done this pass): the spacing/typography *scale* round (subjective; "at least a consistent scale" — a dedicated round), and iconography + the app icon (needs design assets; also a K-workstream store requirement). First-run/empty-state polish is folded into **P5** (onboarding), where it belongs.

---

## Verification

- `py_compile` clean on the four touched files; all import offscreen; full app import OK.
- Offscreen token check: under `light` the resolved gain/loss/muted/status colours equal the old constants exactly; under `dark` they switch to the dark variants (gain `#22c55e`, loss `#f87171`, in-progress `#fbbf24`, muted `#cbd5e1`) — confirming no frozen light hex remains.
- Swept for the adjacent bug class — literal hex inside `tokens.themed(...)` templates (which only substitutes `{token}` placeholders, so a literal would freeze): none found.
