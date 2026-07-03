# ADR-132 — Launch-time dialogs must escape the always-on-top splash

**Date:** 2026-07-03
**Status:** Implemented
**Related:** ADR-103 (the branded launch splash). ADR-109 (the file-recovery dialog shown before the main window). ADR-125 (the sandbox first-run folder picker + the container fallback warning). ADR-099 (the last-resort crash dialog). ADR-050 (cross-platform — this is a Windows-specific stacking bug).

## Context

Owner report (Windows): when there is a problem opening the data file at launch, the error is **invisible** — it sits *behind* the splash screen, so the app just appears to hang with the branded splash up and no way forward.

Root cause is a Windows window-stacking interaction. The launch splash (ADR-103) is created with `Qt.WindowStaysOnTopHint` so it stays visible through a slow launch (DB open + migrations + window build). On Windows that hint places the splash in the OS **"topmost" z-order band**, which sits above *every* non-topmost window — including our own **no-parent modal dialogs**. Every launch-time dialog is parentless (the main window doesn't exist yet), so each one opens underneath the splash and is never seen:

- the **file-recovery** dialog (ADR-109) — file unavailable / unreadable / cloud-evicted;
- the sandbox **first-run folder picker** and the **"couldn't use that location" fallback** warning (ADR-125);
- the last-resort **crash dialog** (ADR-099) — reached if `Repository()` throws during the real open/migration.

This had quietly gotten worse: two of those dialog sites were added *after* the splash, so a per-site fix would keep rotting as new launch dialogs appear.

## Decision

Fix it once, centrally: **any launch-time dialog dismisses the splash before it blocks.**

`mfl_desktop/ui/splash.py` tracks the splash it creates in a module global (`_active_splash`) and exposes **`dismiss_active_splash()`** — idempotent, safe to call with no splash (tests / headless), and it fully `close()`s the splash and clears the reference. Each launch-time dialog site calls it first:

- `__main__._recovery_dialog` (the `dialog_factory` wrapper passed to `launch.resolve_database`) — so `resolve_database` stays window-free and unit-testable (ADR-109); the dismissal lives in the injected factory, not the resolver.
- `__main__._prompt_first_run_location` and `__main__._open_repository_with_fallback` (ADR-125).
- `diagnostics._show_crash_dialog`, which also sets `Qt.WindowStaysOnTopHint` on the message box as belt-and-suspenders (a crash dialog should be topmost regardless).

Once *any* of these fires we are, by definition, in an interactive recovery flow rather than a fast launch, so retiring the branded splash for the rest of that launch is the right trade — correctness over a few hundred ms of branding.

**Rejected:**
- *Drop `WindowStaysOnTopHint` from the splash* — regresses the ADR-103 goal (the splash could sink behind other apps during a slow launch), and two-topmost-window ordering is still not guaranteed.
- *Make each dialog topmost + `raise_()`/`activateWindow()`* — a topmost-vs-topmost race that isn't reliably won on Windows, and it leaves the splash covering the screen behind a small dialog.
- *Wrap each call site to `splash.hide()` inline* — the exact per-site pattern that already rotted when ADR-125 added new dialogs; one shared helper is future-proof (a new launch dialog just calls `dismiss_active_splash()`).

## Consequences

- On Windows, every launch-time error/prompt is now visible instead of hidden behind the splash — the reported "app hangs on the splash" symptom is gone. macOS was not affected (its splash stacking differs) but the same code path is harmless there.
- The happy path is unchanged: with a readable file no dialog fires, nothing calls the helper, and the splash stays up through the open + window build until `splash.finish(win)` (ADR-103).
- New launch-time dialogs must call `dismiss_active_splash()` first — cheap to remember, and the crash-dialog path catches anything that slips through by raising during launch.
- Verified headless (`QT_QPA_PLATFORM=offscreen`): the splash is tracked on creation and stays visible until dismissed; `dismiss_active_splash()` closes it and is idempotent; the crash-dialog and recovery-factory paths each leave no visible splash. The launch-resolution tests are unaffected — they inject their own `dialog_factory`, so the resolver contract is untouched.
