# ADR-102 — A canonical typography scale

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-076 (theming + the macOS px/pt base-font amendment), ADR-026 (visual baseline), `RELEASE_1.0_BACKLOG.md` P4 ("spacing/typography scale — at least a consistent scale, not pixel-perfect").

---

## Context

P4's last open item: the deferred "Arc B" typography round. An audit of every `font-size` in the UI's QSS/themed strings found the sizes already cluster tightly on a small ramp — **10, 11, 12, 18, 20, 22, 30 px** dominate (most used dozens of times) — with only **two genuine one-offs**: a 14 px amount label (transfer confirm card) and a 17 px heading (budget drill-down). So the app was *almost* on a consistent scale already; what was missing was a named source of truth and the removal of the two strays. The backlog explicitly scopes this as "at least a consistent scale, not pixel-perfect," not a re-layout.

---

## Decision

**Define the scale as `mfl_desktop/ui/type_scale.py`** — nine named px steps reverse-engineered from what the app already used, so adoption is descriptive rather than a restyle:

`MICRO 10 · CAPTION 11 · SMALL 12 · BASE 13 · LEAD 15 · SUBTITLE 18 · TITLE 20 · DISPLAY 22 · HERO 30`

(`BASE 13` is the default `*` font on macOS per ADR-076; the steps are the accents around it.) A `SCALE` dict and an `fs(px)` helper (`"font-size: 12px"`) make the scale referenceable from QSS-string builders.

**Fold the two off-scale one-offs onto the nearest step**, referencing the constants as the first live usage: 14 px → `LEAD` (15), 17 px → `SUBTITLE` (18). After this, every `font-size` in the app is one of the nine steps.

New code should reach for a named step; existing on-scale sites were left untouched (no churn, no layout risk) — the scale is documentation + the convention, not a forced migration of all ~75 call sites.

---

## Alternatives considered

- **Tokenise typography like colours and migrate all ~75 sites to `fs(...)`.** Rejected for 1.0 — high mechanical churn and real layout-regression risk for little visual gain when the sizes are *already* on the ramp. The named scale + outlier fix delivers "a consistent scale" at a fraction of the risk; a full migration can come later if desired.
- **Pick a fresh modular scale (e.g. 1.25 ratio).** Rejected — it would re-layout the whole app to chase a theoretical ratio the owner didn't ask for; matching the existing, already-tuned sizes is the "not pixel-perfect" intent.
- **Also formalise a spacing-token system.** Deferred — the app's paddings/margins already sit in a narrow range; a spacing-token round is a separate, optional effort and not required by "at least a consistent scale." Noted, not done.

---

## Consequences

- There is now a single, documented type scale; the app conforms to it end-to-end (no off-scale sizes remain), and new code has a named vocabulary instead of inventing pixel values.
- The change was deliberately minimal (one new module + two edits), so no existing screen shifted.
- A deeper typographic pass (full `fs()` migration, a spacing scale) remains available as a future round but is out of scope for the 1.0 "consistent scale" bar.

---

## Verification

- `py_compile` clean; **import-all = 0 failures**.
- Grep confirms no 14 px / 17 px sizes remain — every `font-size` is one of the nine scale steps `[10, 11, 12, 13, 15, 18, 20, 22, 30]`.
- `type_scale.fs(LEAD)` → `"font-size: 15px"`, `fs(SUBTITLE)` → `"font-size: 18px"`.
