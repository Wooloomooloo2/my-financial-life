# ADR-117 — Brand chrome: logos in the persistent UI

**Date:** 2026-06-27
**Status:** Accepted
**Related:** ADR-100 (brand re-tone — teal `accent` + `brand_gold` tokens). ADR-101 (app icon → `assets/icons/`, `resources.py`). ADR-103 (icon surfaced in About / first-run / splash). ADR-116 (the quick-action toolbar — the other half of the same "UI is text-heavy" feedback).

## Context

The owner's feedback: the everyday UI is "very text heavy," and the MFL + Garelochsoft logos should appear in it. Both marks already existed (`assets/icons/`) and on-brand (teal + gold, ADR-100), but only surfaced in **transient** places — the About box, the launch splash, and the first-run dialog. The persistent surfaces the user looks at all day — sidebar, toolbar, register, status bar — were all text.

The owner picked (from four mocked options): **MFL brand header atop the sidebar + Garelochsoft wordmark in the status bar.**

A complication: the supplied logo PNGs carry a **flat light-blue-grey background**, not transparency. Invisible on the app's light surfaces, but on the dark-theme sidebar (`surface` `#1e293b`) / status bar (`canvas` `#0f172a`) that background renders as an ugly light box.

## Decision

**1. MFL brand header on the sidebar.** `RegisterWindow._build_sidebar_panel` wraps the sidebar tree under a small header — the MFL hexagon mark + "My Financial Life" in brand teal (`accent` token) — sharing the tree's `surface` background with a hairline rule below. The splitter holds this panel in the sidebar's slot; `self._sidebar` is unchanged, so every existing call site still works.

**2. Garelochsoft wordmark in the status bar.** `_add_company_logo_to_status_bar` pins the publisher wordmark (18 px) as a **permanent widget** on the right of the status bar — so transient `showMessage` text never overwrites it — with a "published by Garelochsoft" tooltip. Always present, unobtrusive.

**3. Transparent assets (fixes the dark-mode box).** `tools/make_transparent_logos.py` flood-fills the flat background out from the image **edges** (so internal light details — the gold coin, light-teal facets — are never touched) and writes transparent PNGs: `garelochsoft_logo.png` overwritten in place, and a new `mfl_mark.png` (from `mfl_icon_512`). New `resources.brand_mark(size)` loads the transparent hexagon for **in-UI** chrome, distinct from `app_pixmap`/`app_icon` which derive from the dock/taskbar icon set (left untouched — the packaged app icon is a separate concern). The sidebar header, About, and first-run/splash now use `brand_mark`; `company_logo` already reads the (now-transparent) wordmark. Result reads cleanly on both light and dark surfaces.

## Consequences

- The everyday left rail leads with the product mark instead of bare text, and the publisher gets a permanent but subtle attribution — addressing the "text heavy" feedback without cluttering the register/reports (the data surfaces stay logo-free).
- The dock/taskbar app icon (`mfl_icon_*`, `.icns`/`.ico`) is unchanged — only the in-UI marks gained transparency, via a committed, repeatable tool (the owner can re-run it if new logo art lands).
- View/asset layer only; no migration, no schema change. The transparent knockout is baked into the committed assets — no runtime image processing and no Pillow runtime dependency.
- Verified offscreen on `mfl_public.mfl`: the sidebar header renders the teal mark + wordmark and the status bar pins the Garelochsoft logo (survives a transient status message); `brand_mark`/`company_logo` load non-null with an alpha channel; the knockout composites cleanly on a dark background.
