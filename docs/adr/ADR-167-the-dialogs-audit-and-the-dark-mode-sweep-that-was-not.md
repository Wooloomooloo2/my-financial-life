# ADR-167 — The dialogs audit, and the dark-mode sweep that wasn't

**Date:** 2026-07-14
**Status:** Implemented
**Related:** ADR-076 (design tokens; light **and** dark for every colour). ADR-097 (the dark-mode sweep + dialog button audit — the claim this ADR tests). ADR-161 (found the first survivor: the budget's rich-text Pool line). ADR-166 (the palette that was "tested" and wasn't). ADR-113 (the screenshot harness).

## Context

ADR-161 closed with a stated worry:

> *"Nobody has looked at the ~47 dialogs. They were audited for button order and Esc behaviour (ADR-097) — not for whether they carry the design language. The odds that Account Summary and Budget were the only two surfaces that missed ADR-119 are not good."*

So: audit them. **55 dialog classes across 48 modules.** Each was scanned statically and then **rendered to PNG in both themes** (33 of them are constructible headlessly; the rest need live import/staging state) and reviewed as images.

### The dialogs are fine. The theme layer is not.

The good news first, because it is the answer to the question asked: **ADR-097's dialog audit holds up.** Button order, `QDialogButtonBox` adoption, Esc behaviour, default buttons — all as described. The dialogs are not the stock-Qt disaster Account Summary and Budget were. The `£` literals a grep flags in eight dialogs are **input parsers** stripping a typed symbol, not hard-coded display — no ADR-159 repeat.

The bad news is what the render turned up. ADR-097 also declared the **dark-mode sweep** complete. It is not:

> **73 frozen light-theme hex literals across 20 modules.**

And this is not cosmetic. `reconcile_wizard` — the reconciliation screen, a core workflow — carried five frozen hexes and had never joined the token layer at all. One of them:

```python
_INK = "#0F172A"
title.setStyleSheet(f"font-weight: 600; color: {_INK};")
```

The dark canvas token is `#0f172a`. **The wizard's only instruction line was drawn in the background colour.** In dark mode, "Enter the dates and opening and closing amounts for the statement." was *invisible* — not low-contrast, invisible. It has been since dark mode shipped, and no test could see it because no test looks at pixels.

Two more of the same: `transfer_match_dialogs` and `transfer_destination_dialog` wrap the **transaction amount** — the number you are being asked to confirm — in `<span style='color:#0F172A'>`. The surrounding label *is* themed; the inner span overrides it. So in dark mode the prose is readable and the amount is not.

That is the shape of the whole defect: **`tokens.themed` styles a widget's stylesheet, and rich text puts colour inside an HTML string where the stylesheet cannot reach.** ADR-097 swept stylesheets. Everything living in an HTML attribute, a `QColor` constant, or a module-level style string survived it. ADR-161 found the first survivor by accident; this is the systematic version.

## Decision

**Fix everything that is actually broken in dark mode; ratchet the rest.** (Owner's call, given the size.)

Converted to tokens — each replaced hex's light value equals its token's light step, so **light mode is pixel-identical** and only dark changes:

- **`reconcile_wizard`** — all five constants; the invisible title, the muted captions, and the green/red **Missing** figure that tells you whether the statement ties out.
- **`transactions_list_window`** — `_CHIP_STYLE` was five frozen hexes, so the filter chips stayed a pale slate pill with near-black text: a light island on the dark canvas.
- **`transfer_match_dialogs` / `transfer_destination_dialog` / `transfer_reconcile_dialog`** — the rich-text spans over the amount.
- **`about_dialog`** — the licensed/expired status colours (rich text again).
- **`categories_dialog`** — the archived-row ink, a `QColor` frozen at *import*, which cannot follow a toggle no matter what happens later.
- **`import_mappings_dialog`** — the empty-state label.

**And a ratchet, which is the durable part.** `tests/test_no_frozen_theme_colours.py`:

1. **Dialogs must stay at zero.** A hard assertion — a frozen hex in a dialog is how the reconcile title went invisible.
2. **The rest may only shrink.** Every remaining module is listed with its current count. A number going *up*, or a new module appearing, fails. Add the colour to `tokens.py`; never freeze a new one.
3. **The table can't go stale** — if a module is cleaned up, the test fails until its allowance is tightened, so the ratchet keeps ratcheting.

It scans the **AST**, not a grep: a hex in a *docstring* is prose, not a colour. (The first version of the scanner used naive `#`-splitting and reported **zero** frozen hexes — it had mistaken every `"#94a3b8"` string for a comment. A scanner that reports what you hoped to hear is worth exactly nothing.)

## Rejected

- **Converting all 73 in one arc.** The remaining 60 are chart *series/semantic* colours (income-green, spend-red, the net-worth family hues, the returns-chart ramp). They are **readable** in dark, just not tuned — a different problem from invisible text, needing per-chart judgement, and now a scoped follow-up rather than a rushed sweep.
- **A blanket regex replace.** Some of these hexes are *correct*: white text drawn on a saturated fill is white in both themes (`treemap_chart`, `burn_down_chart`), and Qt's `BrightText` role is white by definition. A blind sweep would have "fixed" those into unreadable text. The ratchet's allow-list records them as deliberate.
- **Trusting the ADR-097 note that this was done.** The whole reason this ADR exists.

## Consequences

- Dark mode is *correct* on the reconciliation wizard, the transfer dialogs, the filter chips and the About box for the first time.
- Light mode is unchanged — by construction, each token's light value is the hex it replaced.
- The remaining 60 frozen colours are **named, counted, and can only decrease**.
- A new dialog cannot introduce a frozen colour without failing the suite.

## Known limitation

**The 60 remaining are not fixed, only fenced.** The charts' series colours still use frozen light-theme hexes on the dark surface. They are legible, so this is debt, not breakage — but ADR-166 has just shown that "legible" and "correct" are different things, and the net-worth window's six family colours in particular are a categorical palette that never met the validator. Filed in the backlog as the follow-up arc.

`tests/test_no_frozen_theme_colours.py` 3/3 — **2 of the 3 fail against the unfixed code**. Full suite 347/347. No schema change.
