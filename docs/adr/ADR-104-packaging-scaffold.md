# ADR-104 — Packaging scaffold: PyInstaller spec, per-OS build scripts, CI matrix

**Date:** 2026-06-21
**Status:** Accepted
**Implements:** ADR-078 (the K0 distribution decision — direct signed+notarised downloads; this is the as-built build machinery it called for).
**Related:** ADR-099 (pinned deps + build metadata), ADR-101 (icon assets — the bundle icons), ADR-050 (file model — `Snapshots/`/`Library/` beside the live file, fine under non-sandboxed direct distribution), `RELEASE_1.0_BACKLOG.md` workstream K.

---

## Context

ADR-078 locked *direct signed+notarised downloads* for 1.0 but noted "no spec/build scripts exist yet." This ADR records the scaffold that turns the source tree into a packaged app on macOS + Windows. The blocking unknown was the **runtime data dependencies** a frozen build must carry: an audit found exactly one critical one — the 31 `mfl_desktop/migrations/*.sql` files, loaded at runtime via a `__file__`-relative path. Without them bundled (and found) the packaged app applies zero migrations and every database stays empty. The license public key is an embedded string (no file); the icon assets already resolve through the `_MEIPASS`-aware `resources.py` (ADR-101); `ofxtools` ships three trivial config files.

---

## Decision

**(1) Make migration loading frozen-aware** (`db/schema.py`). `_migrations_dir()` returns `<sys._MEIPASS>/mfl_desktop/migrations` when frozen, else the source path. This is the one code change packaging required.

**(2) One PyInstaller spec, both targets** (`packaging/mfl.spec`). Run on each OS (PyInstaller can't cross-compile): macOS → `.app`, Windows → folder+exe. It bundles the migrations and `assets/icons` as data, collects `ofxtools` data, sets the per-OS bundle icon (`.icns`/`.ico`), builds a **windowed** (no-console) app, and excludes the unused legacy web stack (fastapi/uvicorn/pyoxigraph/rdflib/pydantic) + heavy optional libs to keep the bundle lean. On macOS it emits a `BUNDLE` with a proper `Info.plist` (version, finance category, high-DPI, bundle id `life.myfinancial.app`). The entry point is a thin `packaging/mfl_main.py` calling `mfl_desktop.__main__.main`.

**(3) Per-OS build scripts** (`packaging/build_macos.sh`, `build_windows.ps1`). Each stamps build metadata (`stamp_build_info.py` → the gitignored `_build_info.py` with git SHA + date, surfaced in About/diagnostics per ADR-099), runs PyInstaller, then does **signing/notarization only when the env vars are set** — so they produce a working *unsigned* `.app`/`.dmg` (and Windows folder/installer) out of the box, and become a one-command signed release once the owner has the Apple Developer ID / Windows cert (ADR-078 K1/K2). macOS additionally builds a `.dmg` (`hdiutil`); Windows optionally drives Inno Setup if present.

**(4) CI matrix** (`.github/workflows/build.yml`). macOS + Windows runners install `requirements-desktop.txt` + `requirements-build.txt`, run the offscreen smokes (IRI guard + import-all), then run the build scripts (unsigned) and upload the artifact. Every push gets a buildable, downloadable binary on both OSes.

**(5) Pin the build tool** (`requirements-build.txt` → `pyinstaller==6.21.0`), separate from the runtime deps.

---

## Alternatives considered

- **Rely on `__file__` for migrations in the frozen app.** Rejected — PyInstaller doesn't guarantee a real filesystem `__file__` for frozen modules; the explicit `sys._MEIPASS` branch is the documented, reliable pattern (and was verified to actually apply all 31 migrations from the bundle).
- **Briefcase instead of PyInstaller.** Not revisited — ADR-078 already settled PyInstaller; the spec works and is verified.
- **onefile build.** Rejected for now — onedir launches faster (no per-run unpack) and is simpler to sign/notarize; size isn't a concern for a direct download.
- **A Linux smoke job in CI.** Skipped — the target OSes are macOS + Windows; running the smokes on those same runners avoids wrestling Linux Qt platform libs for no shipping benefit.

---

## Consequences

- `bash packaging/build_macos.sh` produces a runnable `My Financial Life.app` + `.dmg` today; the same script signs+notarizes once `MACOS_SIGN_IDENTITY`/`AC_NOTARY_PROFILE` are set. Windows is symmetric pending a runner/cert.
- CI gives a per-OS artifact on every push — the reproducible build env ADR-078/P6 wanted.
- **Still open (needs the owner's accounts, not code):** the Apple Developer ID + notarization profile (K1), the Windows code-signing cert (K2), an optional Inno Setup `installer.iss`, and auto-update feeds (Sparkle/WinSparkle). The scaffold has the hooks for all of them.

---

## Verification

- Real macOS build (PyInstaller 6.21.0): `My Financial Life.app` produced (159 MB), with all **31 migrations** and **7 icon PNGs** bundled.
- Launched the frozen binary headless: it starts, logging + version/build metadata work in the bundle (`1.0.0 (ce4b2d8+dirty)`), and against a fresh DB it **applies all 31 migrations from the bundle** — confirming the `_MEIPASS` resolution. (The ad-hoc-codesign warning during BUNDLE is the known macOS xattr "detritus" issue; real signing happens in the build script with an identity.)
- The Windows path is unrun locally (no Windows host) but symmetric; CI exercises it.
