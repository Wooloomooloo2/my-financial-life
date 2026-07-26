# ADR-183 — The register search box gets its own row

**Date:** 2026-07-22
**Status:** Implemented
**Related:** ADR-119 (the register header strip). ADR-061 (the debounced search). ADR-041/062/063/040 (the Show / Filters / Schedules / Reconcile controls that share the strip).

## Context

Owner-reported with a screenshot: the register's Search box had shrunk to a few characters wide — showing only "Se…" — so you couldn't see what you were typing.

The register header was a single `QHBoxLayout` packing, left to right: `Search:` + the search field (`stretch=2`), Show combo, Status combo, Filters button, a stretch, then the New Transaction / Schedules / Reconcile buttons. The combos and three buttons have fixed minimum widths; the search field was the only elastic item, so on a narrow-ish window (or once the buttons grew — the Schedules label carries a due-count, ADR-063) it was squeezed down to its minimum. `stretch=2` only governs *spare* space, and there was none to share.

## Decision

**Lift the search box onto its own full-width row above the filter/action controls.** A new `search_row` (`Search:` label + the field at `stretch=1`, `setMinimumWidth(280)`) sits directly above the existing controls row, which now starts at the Show combo. Both are stacked in the register's right-hand `QVBoxLayout`.

The field now spans the content width (~560–800px at typical sizes) and can never be crowded out by a sibling control, because it no longer shares a row with any. The 280px floor guarantees a usable box even at the narrowest window.

## Rejected

- **Just give the field a larger minimum width on the shared row.** Trades one crowding for another — a wide minimum would instead push the action buttons off the right edge or force the strip to wrap unpredictably. The controls genuinely don't fit on one row with a readable search box; a second row is the honest fix.
- **A max width / centred search bar.** Left-aligned full-width reads as a normal search field and matches the strip's left origin; a capped, floated box would look arbitrary.
- **Moving search into the Filters popover (ADR-062).** Search is the most-used control on the register; demoting it behind a button is the wrong direction.

## Consequences

- The search box is readable at any window width — the reported "Se…" can't recur.
- The register header is now two rows (search, then controls); the table gets marginally less height (one row, ~34px). Acceptable for a control this central.
- Purely a layout reflow: the search widget, its ADR-061 debounce, and every other control are unchanged in behaviour and wiring. Verified by driving the register offscreen at 1180px — the field renders at full width showing the complete placeholder, with Show / Status / Filters / New Transaction / Schedules / Reconcile intact on the row below. `tests/` register suite 5/5, full suite unchanged. No schema change.
