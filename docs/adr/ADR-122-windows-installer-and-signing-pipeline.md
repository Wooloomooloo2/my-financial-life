# ADR-122 — Windows installer + signing-ready release pipeline

**Date:** 2026-06-29
**Status:** Accepted
**Related:** ADR-104 (PyInstaller build system — spec + per-OS scripts + CI; this completes its Windows installer leg). ADR-078 (packaging strategy — direct signed/notarised downloads for 1.0; K2 = code signing). ADR-079 (licensing — the signed-build edition entitlement). ADR-050 (cross-platform rules — user data in `%APPDATA%`, never the install dir).

## Context

The build scaffolding from ADR-104 was nearly complete — a PyInstaller spec, per-OS build scripts, a CI matrix building both OSes, pinned `requirements-desktop.txt`/`requirements-build.txt`, a mature `version.py`, and an icon set. macOS already produced a proper `.dmg`. **But the Windows side stopped at the raw PyInstaller *folder*:** `build_windows.ps1` looked for `packaging\installer.iss`, which didn't exist, so there was no single-file installer — no Start-menu shortcut, no uninstaller, nothing shareable with a non-technical user. This is the last functional gap before a 1.0 Windows release.

### Decisions taken with the owner (two forks)

- **Code signing gates 1.0** (owner will buy a Windows code-signing certificate) — installs should be clean (no SmartScreen "Run anyway"), accepting the cost and the OV vetting delay. The pipeline is built fully signing-ready so issuing the cert is the only remaining step. (Rejected: ship unsigned for friends/family and sign later.)
- **Per-machine install** to `Program Files` with an admin/UAC elevation (the traditional layout), over a per-user no-admin install. (Rejected: per-user install — friendlier UAC-free flow, but the owner wanted the standard Program Files install.)

## Decision

Add **`packaging/installer.iss`** (Inno Setup) and wire it into the build + CI:

- **Per-machine installer:** `DefaultDirName={autopf}\My Financial Life`, `PrivilegesRequired=admin`, `ArchitecturesAllowed=x64` (alias that works across all Inno 6.x), `MinVersion=10.0`. Stable `AppId` GUID so future versions upgrade in place. Start-menu shortcut + uninstaller always, desktop icon as an unchecked task, optional launch-on-finish. `OutputBaseFilename=MyFinancialLife-<version>-setup`.
- **User data is never touched by uninstall:** the `.mfl` files live in `%APPDATA%` via `QStandardPaths` (ADR-050), not under `{app}`, so the `[Files]` set is only the program and uninstall removes only the program.
- **Version + paths are passed in, not duplicated:** `build_windows.ps1` reads `__version__` from `version.py` and passes `/DMyAppVersion`, `/DAppSourceDir`, `/DOutputDir` to `ISCC.exe`, so the version is single-sourced and the installer compiles against the actual PyInstaller output folder. The script has working defaults so it can also be run standalone.
- **Signing-ready, both binaries:** when `WINDOWS_SIGN_PFX` / `WINDOWS_SIGN_PASSWORD` are set, `build_windows.ps1` signs the **bundled app exe before packaging** and the **finished installer after compilation** (SHA-256 + RFC-3161 timestamp). Unset → a working unsigned app + installer (SmartScreen warns), so nothing is blocked while the cert is procured.
- **CI produces the real artifact:** the Windows job `choco install innosetup` then uploads `dist/*-setup.exe` (was the raw folder). CI stays unsigned (no secrets), so it verifies the installer *compiles and is reproducible* every push; the owner's local signed build is the release.

## Consequences

- A double-clickable, upgradable Windows installer exists — the app is now shareable, pending the cert.
- The **only remaining step to a clean signed 1.0** is procuring the cert and setting two env vars; no further code changes. (macOS signing/notarisation is the parallel gate on its side — Apple Developer account — already wired in `build_macos.sh`.)
- Per-machine install means a UAC prompt at install time (the accepted trade for the Program Files layout).
- Build/Inno can't run in the dev sandbox; verified what's checkable here — PowerShell parses clean, the `version.py` query returns `1.0.0`, the `.iss` follows Inno 6 conventions with broadly-compatible directives. The owner runs the actual build on Windows (Inno Setup 6 installed); CI exercises the full compile on `windows-latest`.

### Deferred
- **Auto-update (WinSparkle/Sparkle, ADR-078):** not wired anywhere yet — needs an appcast feed + hosting; a later round.
- A **tag → GitHub Release** job (attach the signed installer + `.dmg` to a release) once signing is live.
