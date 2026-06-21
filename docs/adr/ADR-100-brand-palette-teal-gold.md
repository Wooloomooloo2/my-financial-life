# ADR-100 — Brand palette: re-tone the accent from blue-600 to icon teal, add a gold brand token

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-026 (Fusion + slate/blue-600 visual baseline), ADR-076 (design tokens + live theming — this deliberately amends its accent), ADR-097 (chart series-colour discipline), `RELEASE_1.0_BACKLOG.md` P4 (iconography). Owner-driven: app-icon artwork for My Financial Life + My Retirement Life.

---

## Context

The owner produced app icons for both apps — hexagonal badges on a **deep petrol-teal** field with a **gold** mark (My Financial Life: a gold isometric "M" + coin + up-arrow; My Retirement Life: a gold "R" + sunset + sailboat). Sampling the artwork gives the brand system: teal `#0e3a44`→`#1f6e78`→`#43a0a8`, gold `#c9a23a`→`#e3c560`, with a warm sunset orange that reads as *MRL's* signature (kept out of MFL).

The app shipped on ADR-026's Tailwind-slate canvas with **`blue-600 #2563eb`** as the accent. With a teal+gold brand now defined, the blue accent is off-brand. The owner chose (AskUserQuestion) the **full re-tone**: swap the app-wide accent to teal, add a gold brand highlight, keep semantic green/red for money.

ADR-076 built exactly the machinery this needs: the accent lives as `accent` / `accent_hover` / `accent_subtle` / `on_accent` tokens, and `theme.py` drives the entire QPalette + QSS (buttons, selection, links, focus, menu highlight) off them. So a token-value change re-tones the whole UI at once. The only blue that bypasses tokens is in the paintEvent charts.

---

## Decision

**(1) Re-tone the accent tokens to brand teal** (`tokens.py`). Light/dark:
- `accent` `#2563eb` → **`#1f6e78`** (dark `#3b82f6` → `#39a0aa`, brighter for contrast on the dark surface).
- `accent_hover` → `#185860` / `#2f8893`.
- `accent_subtle` (selection wash) → `#d8edef` / `#1f4248`.
- `on_accent` stays white.

This **deliberately breaks ADR-076's "light value == the hex it replaced" discipline** — that rule preserved the look across the dark-mode rounds; here the *point* is to change the look. The break is scoped to the accent family (and the chart accent literals below); every other token is untouched, so the slate canvas, text ramp, and semantic state colours are unchanged.

**(2) Add a `brand_gold` token** (`#c9a23a` / `#dcbb55`). Gold-as-text fails contrast on the app's white surfaces (~2.3:1), so it is **not** used for body text or money — it's reserved for contrast-safe brand accents (graphic rules/fills) and the icon. Its first use: the About box's divider becomes a gold rule tying the dialog to the icon's gold mark. Semantic positive/negative stay green/red.

**(3) Route the *accent-semantic* charts through a theme-aware helper.** New `chart_helpers.chart_accent()` returns `tokens.c("accent")`. The chart elements that *are* the accent — `balance_flow` balance line, `burn_down` today marker, `income_expense` net line, `payee` bar, `price_history` line/dot, `value_history` invested line — now read `_ch.chart_accent()` at paint time (so they follow light/dark, and a frozen teal isn't left too dark on the dark surface). Their frozen `#2563eb` constants are deleted. The transfer "Strong" chip and the bulk-review summary's inline accent count also move to teal/tokens.

**(4) Leave the categorical/data blues alone** — documented, not overlooked:
- `chart_helpers.GROUP_PALETTE` (the 12-hue stacked-bar palette) keeps blue-600 as entry 0: it already contains teal-500/cyan, so re-coloring it to brand teal would collide in multi-series charts. It's a *data* palette, not the accent.
- `net_worth_window` family colours keep investment = blue-600 (Property is already teal-500 — making investment teal would collide).
- `returns_chart` cost line keeps blue-600 (its "realized gains" series is already teal-600 `#0d9488`).

Blue surviving as a *data hue* alongside teal-as-accent is normal and readable — ADR-097's rule that data-series colours are fixed, theme-independent literals stands.

---

## Alternatives considered

- **Make money/hero numbers gold.** Rejected — gold-on-white is a contrast failure for text, and tinting a money figure conflicts with the green/red semantic. Gold stays a graphic accent.
- **Freeze the chart accent lines to a teal literal** (matching ADR-097's data-series pattern). Rejected for the *accent* lines specifically — brand teal `#1f6e78` is too dark on the dark surface; routing through `chart_accent()` gives the brighter dark-mode teal for free and centralises the accent in one place.
- **Also re-tone GROUP_PALETTE / net-worth / returns blues.** Rejected — collisions with existing teal/cyan palette entries; they're categorical data, not the accent.
- **Keep blue-600, brand only the icon.** Rejected — the owner chose the full re-tone for a cohesive product.

---

## Consequences

- The whole token-driven UI (buttons, selection, links, focus rings, menu highlights, default-button fill) is now brand teal in both themes, from a four-line token change. The accent-semantic chart lines follow.
- `brand_gold` exists for the icon + future teal-surface brand moments; its first UI appearance is the About divider.
- A future brand-colour tweak is again a one-place edit (`tokens.py` + `chart_accent()`), since the charts no longer hardcode the accent.
- Categorical blue remains a data hue — intentional and documented, so a later pass doesn't "finish the job" and break chart legibility.

---

## Verification

- `py_compile` clean on all 11 touched files; **import-all = 0 failures** (charts construct).
- Token resolution: light `accent #1f6e78` / `brand_gold #c9a23a`; dark `accent #39a0aa` / `brand_gold #dcbb55`; `chart_accent()` tracks the accent in both themes; `brand_gold` is a real token (no magenta fallback).
- Rendered the About box (light): default button is teal, the divider is a gold rule, build line present — confirming the palette + QSS picked up the new tokens.
