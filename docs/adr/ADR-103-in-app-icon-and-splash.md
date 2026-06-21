# ADR-103 — Surface the app icon in-app + a branded launch splash

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-101 (app icon + `resources.py`), ADR-100 (brand palette), ADR-098 (first-run dialog), ADR-099 (About box build line). Owner-requested polish.

---

## Context

ADR-101 generated the app icon and wired it as the window/dock/taskbar icon, but it never appeared *inside* the UI — the About box and first-run welcome were text-only, and there was no launch splash. The owner asked whether to show the icon in-app and add a splash / getting-started helper. The chosen scope (AskUserQuestion): **icon in About + first-run**, and a **branded splash** (the Home empty-state "getting started" card was declined — the existing first-run dialog covers onboarding).

---

## Decision

**(1) `resources.app_pixmap(size)`** — a crisp square `QPixmap` of the icon at any size, drawn from the multi-resolution `QIcon`, for showing the mark inside the UI. Null-safe.

**(2) Icon in the About box.** The top of the dialog is now an icon (64 px) + a text column (name / version / build), replacing the text-only header — the standard "About" layout, tied to the gold rule below it.

**(3) Icon in the first-run welcome.** A 48 px icon sits beside the "Welcome to My Financial Life" heading, so the first thing a new user sees is branded, not a bare form.

**(4) A branded launch splash — `mfl_desktop/ui/splash.py`.** `make_splash()` composes a 440×280 light card (icon + name in brand teal + tagline + a "Loading…" line) and returns a `QSplashScreen`. `__main__` shows it immediately after the app icon is set — *before* the slow work (DB open + migrations + window build) — calls `processEvents()` to paint it, and `splash.finish(win)` once the main window is up (so it closes before the first-run dialog, when that fires).

The splash uses **explicit brand literals**, not theme tokens: it's composed before the persisted light/dark theme is applied (that needs the DB open), so it's always a light, brand-coloured launch screen regardless of the user's theme — a deliberate, documented exception to the token discipline for this one pre-theme surface.

---

## Alternatives considered

- **Put the icon on the populated Home dashboard.** Rejected — Home is a dense data view; a logo there competes with the numbers. The icon belongs on brand surfaces (About, first-run, splash), not the working dashboard.
- **A getting-started card on Home's empty state.** Declined by the owner for now; the first-run dialog already covers the new-file onboarding. Easy to add later if wanted.
- **Enforce a minimum splash duration.** Rejected — that adds artificial delay on fast launches. The splash shows for exactly as long as startup takes; a quick launch just flashes it.
- **Use theme tokens for the splash colours.** Rejected — the theme isn't applied yet at splash time; brand literals are correct and intentional here.

---

## Consequences

- The brand mark now appears at the three natural in-app moments — launch (splash), About, and first run — without cluttering the data views.
- The splash gives immediate feedback on a cold start / large file instead of a blank pause before the window paints.
- `app_pixmap()` is the reusable helper for any future in-UI icon use.

---

## Verification

- `py_compile` + **import-all (0 failures)** clean.
- Rendered all three offscreen: the splash (icon + teal name + tagline + "Loading…"), the About box (64 px icon beside name/version/build, gold rule, teal default button), and the first-run welcome (48 px icon beside the heading, teal focus + "Get started"). `app_pixmap(64)` is non-null.
