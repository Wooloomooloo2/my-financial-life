# ADR-099 — Release engineering hygiene: pinned desktop deps, build metadata, local crash log + diagnostics export

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-078 (packaging & distribution), ADR-079 (version.py / About), ADR-050 + ADR-057 (file model + snapshots), ADR-098 (Help menu), `RELEASE_1.0_BACKLOG.md` item **P6**.

---

## Context

P6 is the unglamorous pre-packaging hygiene: pin dependencies for a reproducible build, surface version + build metadata, get a basic automated test pass, and decide a crash/error-reporting approach (the backlog names "local log + export diagnostics button" as the privacy-friendly minimum).

Two findings on audit:

1. **The desktop app's dependencies were not pinned anywhere.** The repo-root `requirements.txt` pins the **legacy v0.1 FastAPI/Oxigraph web app** (fastapi, uvicorn, pyoxigraph, rdflib, starlette, …) and does **not** even list PySide6. The actual desktop runtime surface was undocumented. A scan of every top-level + lazy import across `mfl_desktop/` shows the real third-party set is tiny: **PySide6, cryptography, ofxtools** — the FX and price clients use stdlib `urllib` (no `requests`), and everything else is the standard library.

2. **No logging, no crash handling, no build metadata.** An uncaught exception vanished (no log, no message); there was no `__version__`-plus-build string and no way for a non-technical user to send a useful bug report. `version.py` had `__version__` but no build revision.

---

## Decision

**(1) Pin the desktop dependencies separately.** New `requirements-desktop.txt` pins the three direct runtime deps to the verified versions (`PySide6==6.11.1`, `cryptography==49.0.0`, `ofxtools==0.9.5`) with Python 3.11+ noted. The legacy `requirements.txt` is left untouched (it serves the maintenance-mode web app); a header in each cross-references the other so nobody builds the desktop app off the web-app file. This is the reproducible install surface PyInstaller packaging (ADR-078) will freeze per-OS.

**(2) Build metadata in `version.py`.** New `build_revision()` reads an optional `mfl_desktop/_build_info.py` (a `REVISION` string the packaging CI stamps in), falling back to `"source"` in a plain checkout — it never shells out to git at runtime (fragile in a frozen app). `build_string()` → `"1.0.0 (source)"`. The generated `_build_info.py` is `.gitignore`d. The About box gains a muted "Build {revision}" line under the version.

**(3) Local crash log + exportable diagnostics — `mfl_desktop/diagnostics.py`.** The privacy-friendly minimum, no telemetry:
- `setup_logging()` — a rotating file log (`<AppData>/MFL/logs/mfl.log`, ~1 MB × 3 backups) + console, configured once at launch (idempotent; a read-only home degrades to console-only rather than failing to start). Logs an environment banner on start.
- `install_excepthook()` — routes uncaught exceptions to the log, then shows a best-effort non-fatal dialog (only if a `QApplication` exists) telling the user their data is safe (continuous auto-commit + snapshots), where the log is, and that Help ▸ Export Diagnostics bundles it. Re-entrant-guarded; chains to the previous hook; passes `KeyboardInterrupt` straight through. Both are wired early in `__main__.main()`, right after the app name is set (so the log dir resolves) and before any DB/window work.
- `collect_diagnostics(repo)` / `write_diagnostics(dest, repo)` — a PII-light blob (app + build + Python/Qt + OS, key paths, account count, base currency, and the tail of the log). Help ▸ **Export Diagnostics…** writes it to a user-chosen file. Nothing is sent anywhere — the user attaches the file to a support email.

**(4) Automated test pass.** The `tests/` suite (the ADR-096 IRI guard) stays green; added an import-all + `compileall` smoke as the cheap cross-module regression gate (135 modules import clean offscreen). This is the "basic automated test pass" gate to run per packaged build on each OS, alongside the existing offscreen-Qt smoke pattern.

---

## Alternatives considered

- **Fold desktop deps into the existing `requirements.txt`.** Rejected — it would conflate two apps with disjoint stacks and risk breaking the legacy web app's reproducibility. A separate file is clearer and lets each freeze independently.
- **A hosted crash reporter (Sentry-style).** Rejected for 1.0 — it adds a network dependency and a privacy story (financial-app users chose local-first precisely to avoid data egress). The local-log + user-initiated export is the privacy-aligned minimum the backlog calls for; a hosted option can be an opt-in later if support volume justifies it.
- **Embed the git SHA by running git at startup.** Rejected — a frozen app has no git; a CI-stamped `_build_info.py` is the robust packaged-build path, with a clean "source" fallback for dev.
- **Pin transitive dependencies too (full lock).** Deferred — the direct-pin file is the human-maintained intent; a full `pip freeze` lock per OS belongs with the PyInstaller build scripts (ADR-078), which don't exist yet.

---

## Consequences

- The desktop app now has a documented, pinned, reproducible dependency set — the prerequisite for the per-OS packaging round (ADR-078 / K1-K2).
- A crash is now recoverable-by-support: it's logged locally, the user is told their data is safe and where the log is, and one menu item produces an attachable diagnostics file — without any data leaving the device.
- About + diagnostics carry a build identifier, so a bug report names the exact build.
- **Still open under P6** (with the packaging round, not this pass): per-OS reproducible build env + PyInstaller specs + the full dependency lock, and wiring the import-all/offscreen smokes into a CI matrix on both OSes. These are ADR-078 / K-workstream items.

---

## Verification

- `py_compile` clean on all touched files; **import-all across `mfl_desktop` = 135 modules, 0 failures**; `compileall` clean.
- Diagnostics offscreen: logging writes to the AppData log path; `environment_summary()` / `build_string()` render; `install_excepthook` installs (re-entrant-guarded); `collect_diagnostics(repo)` against a seeded file contains the environment, paths, account count, and log tail. About box builds with the new Build line.
- `tests/test_iri_boundary.py` → 6/6 still green.
