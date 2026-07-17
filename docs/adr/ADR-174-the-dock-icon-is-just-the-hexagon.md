# ADR-174 — The dock icon is just the hexagon

**Date:** 2026-07-17
**Status:** Implemented
**Related:** ADR-117 (the knockout tool, and the scoping decision this reverses). ADR-101 (the app icon from the MFL hexagon badge). ADR-167 (the ratchet — the model for a guard that stops a fixed asset regressing).

## Context

Owner-reported: *"Mac specific: can we make the logo that appears in the dock not have the white background? It should just be a hexagon."*

It was baked in, not a transparency accident. Every pixel of every `mfl_icon_*.png` was **alpha 255**, with an opaque `#E9EDEF` wash out to the corners:

```
mfl_icon_1024.png: corner(0,0) = rgba(234,238,239,255), alpha range 255..255
```

The odd part is that the fix already existed. **ADR-117 built the knockout tool** — a flood-fill from the image edges, so internal light details (the gold coin, the light-teal facets) are never touched — and then deliberately pointed it away from this set:

> The dock/taskbar `mfl_icon_*` set is deliberately left untouched — that's the packaged app icon, a separate concern from the in-UI mark.

That call was right about the *reason* and wrong about the *art*. ADR-117's motivation was a light box on the **dark-theme sidebar**, a surface we draw. But macOS draws the dock icon on the **user's desktop**, which we do not control and cannot assume is light. The same wash that ADR-117 removed from the sidebar was showing on every dark wallpaper in the dock — the identical defect, one surface over, exempted by a boundary that turned out not to exist.

Two separate artefacts feed the dock, and both were boxed:

- **the packaged `.app`** → `assets/icons/mfl.icns`, via `packaging/mfl.spec`
- **running from source** → `resources.app_icon()`, a multi-resolution `QIcon` over the PNG set, handed to `app.setWindowIcon`

## Decision

**Knock out the whole icon set, and rebuild `mfl.icns` from it.** `tools/make_transparent_logos.py` grows two steps; the flood-fill itself is ADR-117's, unchanged.

Four things worth recording, three of which came from measuring rather than reasoning:

**1. Knock out each size independently; do not downscale a transparent master.** This reads backwards — a clean 1024 master ought to produce cleaner children — and it is wrong, because **Pillow's `resize` does not premultiply alpha**. The background colour still sitting in the RGB of alpha-0 pixels bleeds back into the edges on downscale. Measured: downscaling left light fringe pixels at 32px where a direct knockout left **none**. The existing sizes are plain LANCZOS downscales with no hand-tuning to preserve (checked), so nothing is lost by re-knocking each.

**2. The geometry already matches Apple's guidance, so nothing is re-inset.** The artwork occupies **78.3%** of the canvas (`bbox=(111, 52, 913, 975)` of 1024, ~111px margins) against the HIG's ~80%. A full-bleed hexagon would have loomed over its dock neighbours; this one doesn't, so removing the wash is the whole change.

**3. The light rim is design, and it stays.** Knocking out the wash exposes a flat `rgb(209,214,217)` band about 9px wide at 1024. It is not fringe: it is a **different colour** from the wash (which is why the flood-fill stopped at it), it is flat rather than a gradient (so not a glow), and it is visible in the original art if you look. It is a die-cut sticker rim that was invisible only because it sat against a near-identical near-white background. At ~9/1024 it is about **one pixel at dock size**, and it helps the badge hold its edge on a dark wallpaper. Left alone.

**4. `iconutil`, not a hand-rolled writer.** It is the format's own compiler and is on every Mac. The packaged bundle's icon is not the place to discover that a hand-written container got a header field wrong. The tool skips this step with a warning off macOS, so the Windows build can still run it for the PNGs.

The tool is **idempotent** — verified byte-identical on a second run. `knockout` flood-fills over the RGB channels, which survive an alpha-zeroing pass unchanged, so a re-run finds the same region and does nothing. That matters because it overwrites committed assets in place.

`brand_mark`'s docstring is corrected: it claimed to exist *because* `app_pixmap` "derives from the dock/taskbar icon set whose PNGs carry a flat light background that would show as a box on dark surfaces". That reason no longer exists.

## Rejected

- **Retiring `mfl_mark.png` / `brand_mark` now that the icon set is transparent.** They are the same art — `mfl_mark.png` is literally generated from `mfl_icon_512.png` — and ADR-084 says consolidate divergent duplicates, so this was tempting. Kept as a **seam, not a workaround**: the packaged icon answers to Apple's and Microsoft's conventions and may one day grow a platform-shaped backing (a Big Sur squircle), while the in-UI mark answers only to our own chrome. Collapsing them would let a future dock-icon decision silently redraw the sidebar. The docstring now says *that*, instead of a reason that has expired.
- **Giving the icon a macOS squircle background.** What Apple's post-Big-Sur guidance actually asks for, and the opposite of what was asked for ("it should just be a hexagon"). It is the owner's brand and a deliberate choice; noted here so the next reader knows it is a choice and not an oversight.
- **Re-insetting the artwork.** Would have been needed if the hexagon were full-bleed. It isn't — 78.3% against a ~80% target. Measuring first saved a change that would have shrunk the icon for no reason.
- **Rebuilding `mfl.ico` (Windows) in the same pass.** See Consequences — this is a real loose end, deliberately left for the owner rather than silently changing Windows packaging on a Mac-scoped request.
- **Removing the light rim to get a "cleaner" cutout.** It is the designer's line, not an artifact. Deleting brand art because a background change made it visible would be a strange way to honour "it should just be a hexagon".

## Consequences

- **The dock icon is the hexagon**, on both paths — packaged and from source — and on any wallpaper. Verified by compositing over grey, near-black, near-white and mid-blue.
- **Windows runtime changed too, and its packaged icon did not.** `app_icon()` is cross-platform, so the Windows *taskbar* icon (from the shared PNG set) is now transparent — while `mfl.ico`, used by `packaging/mfl.spec` and `installer.iss` for the **.exe and installer**, still carries the wash. That is a new inconsistency created by this change and it is flagged rather than fixed: the request was Mac-scoped, and quietly rebuilding Windows packaging artefacts from a Mac is not a decision to make on someone's behalf. `mfl.ico` is a one-line addition to the tool when wanted.
- The tool now overwrites the committed `mfl_icon_*` set. Idempotent, so a stray re-run is harmless, but the source art is now only in git history — as it already was for `garelochsoft_logo.png` since ADR-117.
- `app_pixmap()` has **no callers** — every in-UI surface (sidebar, About, splash, first-run) goes through `brand_mark`. It survives as `brand_mark`'s fallback. Noticed, not removed.

`tests/test_app_icon_transparent.py` 7/7, using **Qt rather than Pillow** — Pillow is a *tool* dependency and is not installed in the app's venv, so a test importing it would silently not run; Qt reads png/icns/ico natively. Every PNG has transparent corners; **no PNG is fully opaque** (the sharpest form of the bug — `hasAlphaChannel()` was already True before the fix, so the channel's existence proves nothing, only its values do); **the hexagon survived** (a flood-fill that ate the artwork would pass a corner test, so the centre must still be teal and 45–75% of the canvas opaque); no opaque wash survives in the edge bands; `app_icon()` is transparent at all seven sizes; the in-UI mark and `app_pixmap` agree; and every one of the **10 `.icns` slots** round-trips through `iconutil` transparent.

All five applicable guards were confirmed to **fail against the pre-fix art** (restored from git into a scratch tree with `resources._root` repointed at it) — `mfl_icon_16.png corner (0,0) is opaque #e8edee — the white box is back`.

Full suite 410 passed, 0 failed. No schema change.
