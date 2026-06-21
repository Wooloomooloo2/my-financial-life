# ADR-101 — App icon from the My Financial Life hexagon badge

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-100 (brand palette — same artwork), ADR-078 (packaging — consumes the platform icons), `RELEASE_1.0_BACKLOG.md` P4 (iconography + app icon) + K (stores need an icon).

---

## Context

P4 lists "iconography + app icon (needed anyway for stores)," and the owner supplied the brand artwork — a single PNG with both apps' hexagonal badges. The left badge is My Financial Life (gold isometric "M" + coin + up-arrow on petrol teal). The app had no window/dock/taskbar icon and no packaged bundle icon.

---

## Decision

**Extract the MFL (left) badge and generate a full icon set under `assets/icons/`.** A centred square crop around the detected badge bounding box (centre 351,387 in the 1408×768 source) → a 1024 master → the standard PNG sizes (16/32/64/128/256/512/1024) → platform bundle icons:
- **macOS `mfl.icns`** built via `iconutil` from a complete `.iconset` (16…512 + @2x retina variants).
- **Windows `mfl.ico`** — a 6-size multi-resolution ICO (16/32/48/64/128/256).

**Resolve + apply at runtime via `mfl_desktop/resources.py`.** `asset_path()` finds assets under `sys._MEIPASS` when frozen (PyInstaller) or the repo root in a source run; `app_icon()` builds a multi-resolution `QIcon` from the PNG size set (crisp at any size) and never raises if files are missing. `__main__.main()` calls `app.setWindowIcon(resources.app_icon())` right after the app name is set.

The PNGs are the runtime window icon; the `.icns`/`.ico` are the **bundle** icons the packaging step (ADR-078) will hand to PyInstaller for the Finder/taskbar app icon. The artwork lives in `assets/icons/` (versioned — it's a source asset, ~3 MB total).

---

## Alternatives considered

- **Mask the hexagon onto a transparent background.** Deferred — the badge already has its own shape + border and reads well as a square on the light canvas; transparent-corner masking is a polish nicety, not needed for a correct 1.0 icon.
- **Embed the icon as a Qt resource (`.qrc`/`rcc`).** Rejected — a plain `assets/` dir + a `_MEIPASS`-aware path resolver is simpler, needs no build-time `rcc` step, and the same files feed PyInstaller directly.
- **Ship only one PNG and let Qt scale.** Rejected — a multi-size `QIcon` is crisper at small sizes (16/32 in menus and the title bar) than downscaling 1024.

---

## Consequences

- The app now shows the brand icon in the window title bar, dock/taskbar, and ⌘-Tab; the `.icns`/`.ico` are ready for the packaging round so the installed app and store listings have their icon.
- `resources.py` is the reusable, frozen-build-safe asset resolver for any future bundled file.
- The crop/generation was scripted, so regenerating from updated artwork is a re-run, not hand-work.

---

## Verification

- `iconutil` produced a valid `.icns` (`Mac OS X icon … "ic12"`); the `.ico` is a valid 6-icon Windows resource.
- `resources.app_icon()` loads non-null with all seven sizes (16…1024); `asset_path` resolves in source layout.
- Visual check of the 256 px crop: the full badge, centred, even padding.
- `py_compile` clean; app imports offscreen with the icon wired.
