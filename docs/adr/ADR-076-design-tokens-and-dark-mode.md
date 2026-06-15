# ADR-076 — Design tokens + light/dark theming (Arc B round 1)

**Date:** 2026-06-15
**Status:** Accepted
**Amends:** ADR-026 (visual baseline — Fusion + palette + QSS; chart-engine = paintEvent).
**Related:** every UI module with inline `setStyleSheet`; the paintEvent charts.

---

## Context

ADR-026 set a light visual baseline: Fusion + a Tailwind-ish QPalette + a small global QSS in `ui/theme.py`, with per-window `setStyleSheet` allowed to layer on top. In practice the per-window styling sprawled — ~15 UI files hardcode **hundreds** of hex literals — and drifted: the baseline used a *gray* ramp (`#6b7280`, `#374151`) while the feature windows used the true Tailwind *slate* ramp (`#64748b`, `#334155`, `#0f172a`), so there were two different "slate-500"s in the app, plus casing mismatches (`#64748b` vs `#64748B`). There was no way to add a dark theme without editing every file.

Owner decisions (`AskUserQuestion`): Arc B round 1 is the **design-system foundation** (consolidate into shared tokens, fix the inconsistencies, one reusable card style), **and dark mode is in scope** — a light/dark toggle.

---

## Decision

### A semantic token layer (`ui/tokens.py`)

One module owns the palette as **named semantic tokens** (`canvas`, `surface`, `surface_alt`, `border`, `text`, `heading`, `muted`, `subtle`, `accent`, `accent_subtle`, `positive`, `negative`, `warning`, …), each with a **light** and a **dark** value. `c(name)` returns the active theme's hex.

**Discipline that makes the refactor safe:** every token's **light value equals the hex it replaces**, so light mode is pixel-identical after the sweep (zero regression by construction) — only the new *dark* values are a fresh design. The gray-ramp baseline values are unified onto the slate ramp as part of this (the intended consolidation; a barely-perceptible gray→slate shift in light mode).

### Live theme switching without per-widget rewiring

Two mechanisms, both re-run on a theme change:

1. **Global** — `theme.py` rebuilds the QPalette + global QSS from `tokens.c(...)` for the active theme. Re-applying them re-themes every standard widget (windows, dialogs, inputs, buttons, menus, tables, trees, scrollbars) for free.
2. **Per-widget inline styles** — a small **themed-stylesheet registry**: `tokens.themed(widget, "color: {muted}; font-size: 11px;")` formats the template with the active tokens, sets it, and registers the `(widget, template)` in a `WeakKeyDictionary`. On a theme change every registered widget is re-formatted and re-set. This handles arbitrary colour+size+weight combos (which a fixed CSS-class vocabulary can't) and gives live switching with no per-widget signal wiring; dead/destroyed widgets are skipped and GC'd out.

`tokens` also exposes a `ThemeNotifier` singleton with a `changed` signal so paintEvent charts can `update()` on switch.

### Charts read structural colours from tokens

The series palette (`chart_helpers.GROUP_PALETTE`) stays — those saturated mid-tones read on both backgrounds. The **structural** colours (axis text, gridlines, baselines, plot background) move to `tokens.c(...)` and each chart connects its `update()` to `ThemeNotifier.changed`, so charts repaint into the active theme.

### The toggle

`theme.py::set_theme(app, name)` persists the choice to the `setting` table (`ui_theme`), updates the token state, re-applies palette + QSS, and the registry/notifier propagate the rest. `apply_theme(app)` on launch reads the persisted choice (default light). A **View ▸ Appearance ▸ Light / Dark** menu in the register window drives it. No migration — `setting` exists (ADR-035).

---

## Consequences

- One source of truth for colour; the two-ramp + casing inconsistencies are gone.
- Dark mode works app-wide and switches live, because the bulk is palette/QSS-driven and the inline styles route through the registry.
- New UI should use `tokens.c(...)` / `tokens.themed(...)` instead of hardcoded hex — future-proof for both themes.
- Light mode is unchanged by construction (token light values == prior hexes), so the large sweep carries minimal visual risk.

### Round 2 (2026-06-15) — charts + account-summary themed

The B1 light "islands" are now themed, completing dark mode everywhere:

- **`chart_helpers` gained structural-colour accessors** — `chart_surface` / `chart_grid` / `chart_axis_ink` / `chart_ink` / `chart_faint` / `chart_tooltip_bg` / `chart_tooltip_ink`, each returning a token at paint time. The series palette (`GROUP_PALETTE`) stays (its saturated mid-tones read on both backgrounds). All ~12 paintEvent charts had their structural `QColor("#…")` calls converted to these accessors (plot background, gridlines, axis text, separators, tooltip); white-on-coloured-fill text (labels on bars/tiles, the amber today-pill) deliberately stays white.
- **`account_summary_window`** (its `_COLOR_*` constants + inline `setStyleSheet` + paintEvent) converted to `tokens.themed()` / `tokens.c()` like the rest.
- **`theme.apply_theme` now force-repaints all widgets** (`app.allWidgets()` → `update()`) after re-applying, so the paintEvent charts (which read tokens at paint time) redraw into the new theme on a live toggle without per-chart signal wiring.

### Scope / deferred
- This round consolidates colour. A spacing/typography **scale** (tokens exist but only colour is swept thoroughly) and per-screen layout refinement (e.g. the budget screen) are later Arc B rounds.
- A system "auto" mode (follow OS light/dark) is deferred; the toggle is explicit.

### Method note

The bulk of the inline-style conversion (~140 call sites across ~30 files) was done by two bounded regex sweeps (single-line, then multi-line concatenated-literal `setStyleSheet` calls), mapping each hardcoded hex to the token whose **light** value equals it — so light mode is unchanged and only the dark values are new. Dynamic/`f-string` colours and constant-based/paintEvent files were handled by hand or deferred (above).

**Brush-styled widgets** (the sidebar sets section-header and closed-account colours as item `QBrush`es, not via QSS, so neither the global re-apply nor the `themed` registry reaches them): the sidebar resolves those from tokens and re-applies them on `tokens.notifier.changed` via a small `_restyle` walk. The "odd blue strip" around the current sidebar row on macOS was the **native focus ring**, which QSS `outline: 0` does *not* remove — `setAttribute(Qt.WA_MacShowFocusRect, False)` on the sidebar tree (and the register table) does; it's harmless on other platforms.

### Rejected alternatives

- **A fixed CSS-class vocabulary** (`QLabel[cssClass="muted"]`) instead of the `themed()` registry — clean for pure colour, but the app's inline styles mix colour with size/weight/letter-spacing in many combinations, which would need a sprawling class list; the template registry is simpler and exact.
- **Apply-on-restart dark mode** — simpler (no live re-style) but worse UX; the registry makes live switching cheap.
- **Keeping the gray ramp** — perpetuates the two-ramp inconsistency; unifying on slate is the consolidation.

---

## Verification

Offscreen: tokens resolve per theme; `themed()` re-formats registered widgets on `set_theme` and prunes dead ones; `apply_theme`/`set_theme` apply light and dark palettes + QSS without error; the register window, home dashboard, and a sample of report/summary windows build in **both** themes; charts build and repaint on `ThemeNotifier.changed`; the `ui_theme` setting round-trips and is honoured on launch.
