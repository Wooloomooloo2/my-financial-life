# ADR-003 — Packaging strategy

**Date:** 2026-05-18
**Status:** Accepted

---

## Context

My Financial Life must be distributable to non-technical end users on Windows, macOS, and Linux without requiring them to install Python, manage virtual environments, or run terminal commands.

---

## Options considered

### Option 1 — PyInstaller + AppImage (chosen)
Consistent with My Retirement Life. PyInstaller bundles the Python interpreter, all dependencies, and application code into a single executable per platform. AppImage is used for Linux as it produces a self-contained executable that runs on any modern Linux distribution without installation. A single build toolchain covers all three platforms.

### Option 2 — Docker
Rejected. Requires Docker Desktop on the user's machine, adds significant overhead for a local-only application, and is unfamiliar to the non-technical target demographic.

### Option 3 — Electron wrapper
Rejected. Adds a full Chromium browser and Node.js runtime, dramatically increasing bundle size. Unnecessary given the app already runs in the user's default browser.

---

## Decision

**PyInstaller (Windows `.exe`, macOS `.app`) + AppImage (Linux), consistent with My Retirement Life.**

---

## Consequences

- Distribution is a single downloadable file per platform.
- Unsigned macOS builds will show a Gatekeeper warning on first launch until code signing is implemented.
- The Oxigraph TTL ontology files are bundled as data assets alongside the executable.
- PyInstaller configuration (`.spec` file) to be defined when packaging is implemented.
- Both MRL and MFL will eventually share a build pipeline for consistency.
