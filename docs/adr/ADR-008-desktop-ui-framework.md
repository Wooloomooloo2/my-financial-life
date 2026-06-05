# ADR-008 — Desktop application UI framework

**Date:** 2026-06-05
**Status:** Accepted
**Supersedes:** ADR-002 (HTMX + Tailwind + DaisyUI frontend stack)

---

## Context

My Financial Life v0.1 shipped with a browser-based frontend (HTMX + Tailwind + DaisyUI served by FastAPI). Real use of the v0.1 register has revealed two problems that no incremental polish on that stack will fully solve:

1. The transaction register is the central surface of the application and needs to feel as fluid as Banktivity's. A browser-rendered table with HTMX partial updates cannot match a native, virtualised data grid for editing latency, keyboard ergonomics, scroll behaviour, and column resize/sort feel on large registers.
2. The application is intended to be shared with non-technical users as a packaged desktop application alongside My Retirement Life. "Open a browser, navigate to localhost" is a poor first-run experience for a personal-finance app.

Distribution priority is **Windows first** (the owner is personally moving from macOS to Windows), with macOS and Linux to follow. Single-file binary distribution is a hard requirement so that non-technical users can download one file and run it. The owner is the sole developer, writes Python comfortably, and has no preference between major framework options provided the rationale is recorded.

---

## Options considered

### Option 1 — PySide6 (Qt for Python) (chosen)

True native widget toolkit, official Python bindings to Qt 6, LGPL licensed. Qt's Model/View architecture (`QAbstractTableModel` + `QTableView`) is purpose-built for high-performance, editable, virtualised tables — the exact surface where the current stack falls short. QtCharts covers dashboard visualisation; `pyqtgraph` is available if interactive performance becomes critical. Builds to a single Windows `.exe` via PyInstaller or Nuitka. All existing Python code — CSV/OFX/QFX parsers, import staging, classification, duplicate detection — transplants directly with no rewrite.

### Option 2 — Tauri (Rust shell + web frontend)

Small bundle (~10 MB on Windows via WebView2), modern, popular. Rejected because:
- The motivation for moving away from HTMX is precisely to escape the "web inside a window" feel; Tauri still renders the UI in a WebView and inherits the data-grid limitations of HTML/CSS.
- A Rust backend would force a full rewrite of the import engine (a feature the owner explicitly wants to keep as-is). Keeping the Python backend means running it as a sidecar process, which complicates single-binary packaging and introduces IPC overhead.
- WebView2 is built into Windows 11 but adds a runtime install step on older Windows 10 systems.

### Option 3 — Electron

Rejected. Maximises the "web inside a window" feel that this decision is moving away from, ships a full Chromium runtime (~150 MB), and offers no advantage over Tauri for this workload.

### Option 4 — Flutter desktop / .NET MAUI / Avalonia

Rejected. Each would force a rewrite away from Python and discard the existing import-engine investment, in exchange for benefits that do not meaningfully exceed what PySide6 already delivers for this application.

### Option 5 — Continue with HTMX + browser

Rejected. Caps the register feel below Banktivity's, and the localhost-in-browser pattern is the wrong user experience for a packaged desktop application aimed at non-technical users.

---

## Decision

**PySide6 (Qt for Python) as the UI layer, packaged as a single Windows `.exe` via PyInstaller.** macOS and Linux packaging will follow once the Windows build is stable; the same toolchain covers all three.

---

## Consequences

### Positive
- Native widgets on Windows (Fusion / Windows 11 style), giving the register the feel and ergonomics the web stack could not deliver.
- Existing Python parsing and import logic is reused unchanged.
- Single-language stack — no JavaScript, no Rust, no separate frontend build pipeline.
- Qt's Model/View pattern fits the register's needs precisely: virtualised rendering, in-place editing, sortable/resizable columns, and row selection are all idiomatic rather than hand-rolled.
- QtCharts and `pyqtgraph` provide a clear path for dashboard and per-lot performance visualisations.

### Negative / accepted trade-offs
- Larger bundle than Tauri (~60–100 MB for PySide6 + dependencies bundled by PyInstaller).
- LGPL imposes constraints if commercial closed-source distribution is ever required (use dynamic linking; ship the user's right to relink against modified Qt). Acceptable given current personal / share-with-friends use.
- The existing FastAPI HTTP layer, Jinja templates, and HTMX endpoints are no longer the primary UI. They remain available for an optional headless/HTTP mode if useful but will not be maintained as the main interface.

### Implementation notes (non-binding)
- The register on top of a `QTableView` with a custom `QAbstractTableModel` should be built first, since it is the highest-risk surface and validates the choice.
- Storage backing the register is covered by ADR-009.
