# ADR-161 — Account Summary and Budget join the design system

**Date:** 2026-07-13
**Status:** Implemented
**Related:** ADR-119 (the app header / page header / button variants — the design language these two windows never adopted). ADR-102 (type scale). ADR-097 (the dark-mode sweep that missed the rich-text label). ADR-159 (`chart_helpers.currency_symbol()` — the one definition of the glyph). ADR-076 (tokens, light + dark). ADR-034 (the original Account Summary layout). ADR-058 (budget, goals, the perimeter pool).

## Context

The first design review conducted by **looking at the app** rather than reading the code. Every main screen was rendered out of `mfl_public.mfl` (the screenshot harness, `WA_DontShowOnScreen`) and the images reviewed side by side.

P4 declared visual polish complete on 2026-06-21. It was complete *as scoped* — but the scope was a list, not the screens, and the screens say something the list never did:

> **The app does not look like one app.**

Home, the register and the report windows carry the design language: teal accent, card surfaces, the ADR-102 type scale, a real dark theme, `PageHeader` (ADR-119) opening each screen. **Account Summary and Budget carry none of it.** They render as native Fusion — a native tab bar, six identical grey push buttons, a default table grid, and the accent colour nowhere on the screen. They are two of the surfaces a paying user spends real time in, and they are the two that look unfinished.

The cause is chronology, not neglect: both windows predate ADR-119. Nothing in them is *wrong*; they simply kept doing what they did before the design system existed, and no one had looked at them next to Home since.

Looking closely turned up four defects that a code reading would not have surfaced:

1. **Nothing in the app styled `QTabWidget` at all.** Not one rule, anywhere. So every tabbed surface fell back to Fusion's native tab bar — the single loudest reason Account Summary reads as a different application. This is a *global* gap that happened to show up here first.
2. **Budget printed money as `Pool: GBP 822.64`** — the ISO code, where every other screen prints the symbol. `currency_symbol()` (ADR-159) has been the single definition of the glyph since the day before this review; the budget window was the last surface not using it.
3. **A goal target date rendered "by Jun 49".** `_fmt_month` formats `2049` as `49`. That is *correct* for the matrix column headers, where twelve columns all sit in one year and the reader supplies the century — and *wrong* for the one place the app prints a month decades out. A 2049 mortgage payoff reads as 1949.
4. **The Pool/Assigned/Unallocated line carried three frozen light-theme hexes** (`#b91c1c`, `#15803d`, `#b45309`). It is a `QLabel` in **rich text**: its colours live inside an HTML string, not a stylesheet, so `tokens.themed` cannot reach them and the ADR-097 dark-mode sweep — which swept *stylesheets* — walked straight past it. It has been wrong in dark mode since dark mode shipped.

The Account Summary title was also built from `font.pointSize() + 8`: an off-scale size, which is exactly what ADR-102's type scale exists to prevent.

## Decision

**Adopt the existing design system in both windows. Build nothing new.**

Every piece needed was already in the repo and simply unused here — `PageHeader`, `mflVariant`, the type scale, `currency_symbol`, the tokens. The one genuinely new thing is a theme rule for tabs, and that belongs to the theme, not to a window.

**1. Tabs, in `theme.py` (global).** Flat underline tabs: a quiet strip, the accent marking the selected tab, and the pane drawn as the same card the rest of the app is built from. This fixes Account Summary and every future tabbed surface at once.

**2. Both windows open with `PageHeader`** (title + subtitle + an action slot), like every other screen:
- Account Summary: the account's name over *what it is* and *what it's denominated in* — "US Brokerage / Investment · USD" — mirroring the register's "Everyday Current / GBP". The label comes from `account_types.by_storage(...).label`, the canonical map, not from string-munging the storage value (`investment_std` → "Investment std" was the first, wrong, attempt).
- Budget: "Budget" over the budget's name and period. It previously had **no title at all** — it opened straight onto a toolbar.

**3. The budget's six grey verbs become a hierarchy.** `New… Duplicate… Rename… Delete Period… Set up…` were six identical buttons with no rank, which is most of why the screen read as unfinished. Now: the picker, one filled primary **`+ New…`** (creating a budget is the call to action, and the empty state already told you so), and everything else behind a single **`Manage`** menu — with **Delete** below a separator, because it was previously one stray click from Duplicate.

**4. The rich-text info line is rebuilt from tokens on every theme change.** Split into `_paint_info()` and hung off `tokens.notifier.changed`. Resolving the tokens at *render* time alone would not have been enough — the label would then be correct on open but stale after a live toggle, because a theme switch re-applies stylesheets and repaints charts, and this label is neither.

**5. `_fmt_month_long()` for a date that stands on its own** — four-digit year. `_fmt_month` keeps the two-digit form for the column headers, where it is right. The two formats are now named for what they are, and the docstrings say which is which.

## Rejected

- **A `QTabWidget` subclass, or per-window tab styling.** The gap was global. Styling it in the window that happened to expose it would have left the next tabbed surface to rediscover the same thing.
- **Keeping the six buttons and merely recolouring them.** Colour is not hierarchy. Six equally-weighted buttons remain six equally-weighted buttons, and Delete stays next to Duplicate.
- **Making `Set up…` the primary action.** It configures a budget once; it is not what you come to the screen to do. `New…` is the action the empty state already points at.
- **A blanket `s/GBP /£/` and moving on.** The ISO code was the *visible* half. The frozen hexes and the two-digit year were sitting in the same two files and would have survived a cosmetic pass — which is precisely how ADR-159's wrong-number bug got filed as a cosmetic one.
- **Rewriting the budget matrix or the holdings table.** Both are good; they were only ever framed badly. Nothing about the data, the model or the layout changed.

## Consequences

- The two windows now read as the same app as Home. Verified by re-rendering the screens and comparing against the pre-change images.
- **Every tabbed surface in the app changes appearance**, not just Account Summary — the tab QSS is global. This is intended.
- Dark mode is *correct* on the budget info line for the first time.
- `Set up…`, `Period…`, `Duplicate…`, `Rename…` and `Delete` are one click deeper than before (inside `Manage`). Accepted: they are occasional management verbs, and the empty-state text now names the path (`Manage ▸ Set up…`).
- The budget window gained a `tokens.notifier.changed` connection. It is a `QMainWindow` that outlives its renders, so the connection's lifetime is the window's — but this is the ADR-156 class of hazard (an emit into a dead object), so it is worth naming rather than leaving implicit.
- No schema change. No behaviour change to any number: the matrix, the pool, the goals and the holdings engine are untouched — this ADR moves pixels and formats strings.

`tests/test_design_system_adoption.py` 8/8 (symbol not ISO code, sign outside the symbol, the unknown-currency fallback, both month formats, the info line's tokens differing by theme, the tab QSS existing and using the accent). Full suite 304/304.

## Known limitation

The three defects here were found because someone looked at two screens. **Nobody has looked at the ~47 dialogs.** They were audited for *button order and Esc behaviour* (ADR-097) — not for whether they carry the design language. The odds that Account Summary and Budget were the only two surfaces that missed ADR-119 are not good.
