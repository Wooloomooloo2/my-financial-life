# ADR-192 — Linux ships as a native package, not a checkout

**Date:** 2026-08-19
**Status:** Implemented
**Implements:** ADR-104 (the packaging scaffold this extends to a third OS), ADR-078 (distribution).
**Related:** ADR-099 (build metadata), ADR-101 (icon assets), ADR-050/109 (where user data lives — untouched by any of this).

## Context

The app has run on Linux since the pivot, but only the way a developer runs it: clone, make a venv, `pip install -r requirements-desktop.txt`, `python -m mfl_desktop`. `packaging/mfl.spec` already froze cleanly for macOS and Windows and needed nothing new for Linux — what was missing was everything *around* the frozen folder: a launcher a desktop environment can see, an icon it can draw, and an artifact a person can install.

Linux has no single answer to "install this app". The three that matter:

- **`.deb`** — what Debian/Ubuntu users expect. `apt install ./file.deb`, then it's in the applications menu, and `apt remove` takes it away.
- **AppImage** — one file, `chmod +x`, double-click. No install, no root, works on distros the `.deb` doesn't serve.
- **A tarball** — extract and run, for everything else and for people who don't want root.

All three wrap the *same* PyInstaller output, so supporting all three costs one build script rather than three build systems.

## Decision

**One script, `packaging/build_linux.sh`, produces all three** — the Linux counterpart to `build_macos.sh` / `build_windows.ps1`, from the same `packaging/mfl.spec`. Unlike those, it needs no accounts or certificates: Linux has no Gatekeeper or SmartScreen, so an unsigned build is a first-class build, and the script produces installable artifacts on a clean machine out of the box.

**(1) The frozen output is the slug on Linux** (`mfl.spec`): `dist/my-financial-life/my-financial-life`, not `dist/My Financial Life/`. The binary becomes a command on `$PATH` and appears in `/opt`, an AppDir and a tarball; spaces in all of those are a running annoyance (they broke `dpkg-shlibdeps` while this was being built). macOS and Windows keep the display name. The user-visible name comes from the `.desktop` file's `Name=`, which is where a desktop environment reads it from anyway.

**(2) `/opt` + a `/usr/bin` symlink for the `.deb`.** The bundle is a self-contained third-party tree (FHS 3.0 §3.13), not distro-built binaries to scatter through `/usr`. `/usr/bin/my-financial-life` symlinks into it; PyInstaller resolves `_internal/` from `/proc/self/exe`, which follows symlinks, so no wrapper script is needed. Uninstall removes only what the package owns — user data lives in `~/Documents`, `~/.local/share/MFL` and `~/.config/MFL` (ADR-050/109) and is never inside `{app}`, exactly as on Windows.

**(3) Dependencies are computed, not hand-listed.** PyInstaller bundles Python and Qt, but the bundled Qt still links the system's GL/X11/glib/wayland stack. `dpkg-shlibdeps` reads the real ELF `NEEDED` entries and maps them to packages — 88 of them, including ones a hand-kept list would never have guessed and would silently rot. Two wrinkles it needed: files with an unsatisfiable link are skipped (PySide6 ships a Qt TIFF plugin built against `libtiff.so.5`, gone since Ubuntu 23.04 — it can't load whatever we depend on, and `dpkg-shlibdeps` treats a missing library as fatal), and substvars like `libkrb5support0 (= ${binary:Version})` are reduced to the bare package name, since only `dpkg-gencontrol` expands those and a literal `${…}` in `DEBIAN/control` makes the package uninstallable. `FALLBACK_DEPENDS` covers a build host without `dpkg-dev`.

**(4) The app declares its own desktop file name** (`__main__.py`: `setDesktopFileName("my-financial-life")`). This is what Wayland and X11 match a running window to its launcher by. Without it GNOME draws a generic icon next to the app in the dock even though the icon is installed correctly. It is a no-op on macOS and Windows.

**(5) CI builds on `ubuntu-22.04`, deliberately not `-latest`.** The versioned dependencies come from the build machine, so the oldest supported release is the right one to build on — a package built on 24.04 will not install on 22.04, but the reverse works.

## Rejected

- **A `.deb` only.** It is the right *primary* artifact (and the owner's own machine is Ubuntu), but it strands every non-Debian distro. The AppImage and tarball are nearly free once the frozen folder exists.
- **Flatpak / Snap.** Both mean a different build system, a runtime to target, and a store account to publish through — a lot of machinery for a local-first app whose whole story is "it's your file on your disk". Reconsider if it ever goes to a Linux storefront.
- **`onefile` PyInstaller for the "portable" case.** The AppImage already is one file, and gets there without paying the per-launch unpack cost of a onefile bundle. Consistent with ADR-104's onedir choice.
- **Registering `.mfl` as a MIME type** so double-clicking a data file opens it. The entry point takes `--db PATH`, not a positional file, so `Exec=… %f` would hand it an argument argparse rejects. Worth doing later; it needs a small change to `__main__` first, not a packaging change.
- **Hand-written `Depends:`.** Tried first, kept as the fallback. It was wrong within minutes of comparing it to what the bundle actually links.
- **Vendoring `appimagetool` into the repo.** It is a 10 MB binary that changes upstream; the script fetches it on demand, caches it in `build/`, and — if there is no network and none on `$PATH` — skips the AppImage with a message while still producing the `.deb` and tarball.

## Consequences

- `./packaging/build_linux.sh` on a clean Ubuntu produces `my-financial-life_1.0.0_amd64.deb` (60 MB), `my-financial-life-1.0.0-x86_64.tar.gz` (77 MB) and `My_Financial_Life-1.0.0-x86_64.AppImage` (71 MB). No venv, no Python, no terminal for the person installing it.
- Every push now gets a Linux artifact from CI alongside the macOS and Windows ones.
- **The `.deb` inherits the build host's dependency versions.** Building on a newer Ubuntu than the target silently produces a package that won't install there. This is why CI pins 22.04, and it is the one thing to remember when building releases by hand.
- **The AppImage needs FUSE on the user's machine.** It runs on Ubuntu 24.04 as built, but a distro without `libfuse2` needs `./…AppImage --appimage-extract-and-run`. The `.deb` and tarball have no such requirement.
- Three artifacts to smoke-test per release instead of one. They come from a single frozen folder, so a defect in the app shows up in all three; what differs is only whether the launcher, icon and menu entry are wired correctly.
- `Maintainer:` defaults to `Garelochsoft <support@garelochsoft.com>` — override with `MFL_DEB_MAINTAINER` if that address isn't real.

## Verification

Built and exercised on Ubuntu 24.04 (x86_64, GNOME/Wayland), PyInstaller 6.21.0:

- **The frozen app works**: launched with an isolated `HOME`, it applied all **36 migrations from the bundle**, created `~/Documents/My Financial Life/MyFinancialLife.mfl`, wrote its log and took a snapshot — the `sys._MEIPASS` resolution ADR-104 established, holding on a third OS.
- **Real desktop launch**: the packaged binary started on the live Wayland session and loaded the **bundled** Qt Wayland platform plugin (`Successfully loaded Qt platform plugin "wayland"`), i.e. the plugin set ships and resolves from inside the bundle.
- **AppImage**: ran end-to-end from the single file (fresh DB created, all migrations applied).
- **Tarball**: `install.sh` installed into `~/.local/{opt,bin,share}`, the generated entry passed `desktop-file-validate` with an absolute `Exec=`, and `--uninstall` removed every installed path.
- **`.deb`**: correct structure (`/opt` tree, `/usr/bin` symlink, `.desktop`, six hicolor icon sizes, `copyright`), 88 computed dependencies, no unexpanded substvars. Installing it system-wide is the owner's step — nothing here required root.
- Full test suite: 72 of 73 pass. `tests/test_budget_drilldown_editable.py` segfaults during GC under offscreen Qt on this machine **identically on unmodified `master`** (5/5 runs both ways) — pre-existing and environmental, not from this change.
