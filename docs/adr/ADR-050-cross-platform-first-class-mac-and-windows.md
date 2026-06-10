# ADR-050 — Cross-platform as a first-class goal (macOS and Windows)

**Date:** 2026-06-10
**Status:** Accepted
**Amends:** ADR-003 (packaging strategy), ADR-004 (cross-platform portability approach), ADR-008 (desktop UI framework — "Windows-first" stance)

---

## Context

ADR-008 set distribution priority as **Windows-first** because the owner was personally moving from macOS to Windows. That has reversed: development is now happening on **macOS** again, and the app must run and ship on **both** macOS and Windows as equal targets — the owner develops on whichever machine they are at, and will share the packaged app with non-technical friends and family on either OS.

A porting audit of `mfl_desktop/` (2026-06-10) found the application is already almost entirely platform-clean, by virtue of the PySide6 + SQLite + `pathlib` + Fusion-style choices made in ADR-008/009/026:

- **Zero** `sys.platform` / `os.name` / `platform.system()` branches.
- **Zero** hardcoded Windows paths, drive letters, or `%APPDATA%`-style env vars; all path construction is `pathlib.Path`.
- **Zero** OS-shell coupling — no `os.startfile`, `subprocess`, `QProcess`, tray icons, or non-`QFileDialog` native dialogs.
- Date formatting already avoids the Windows-only `%-d` directive (uses the `f"{d.day} {d.strftime('%b %Y')}"` workaround throughout).
- `theme.py` applies the **Fusion** style, which renders identically on all three OSes and honours the custom QPalette (ADR-026).

The app will therefore `pip install` and launch on macOS with **no code change**. What is *not* yet right is (a) native platform *feel*, (b) data-file location hygiene, and (c) the fact that **neither** platform is actually packaged yet (no PyInstaller spec, no build scripts exist). The audit also found genuine drift from ADR-004, which already prescribed OS-appropriate data directories — the desktop app writes its database to the current working directory instead.

The risk this ADR addresses is **regression**: the codebase is clean today, but without a written, enforceable standard it is easy to introduce a Windows-ism (or a Mac-ism) on the next feature and not notice until a friend on the other OS hits it. This ADR makes cross-platform a first-class, *maintained* property rather than an accident of past choices.

---

## Decision

**Cross-platform support for macOS and Windows is a first-class project goal. Both are equal, supported targets. The "Windows-first" framing of ADR-008 is retired.** Linux remains best-effort (the same rules keep it working, but it is not a release target unless asked).

This is enforced by a fixed **rule set** that all `mfl_desktop/` code must follow, plus a small set of work items to close the current gaps and a per-platform release path.

### The rule set (binding on all `mfl_desktop/` code)

These are the rules that keep the codebase portable. They are duplicated into the *Known pitfalls — carry forward* section of `CLAUDE_CONTEXT.md` so they are seen at the start of every session.

1. **Paths: always `pathlib.Path`.** Never build paths with string concatenation, `/` or `\` literals, or `os.path.join` on hand-typed separators. Never hardcode a drive letter, a home directory, or a `%APPDATA%` / `~` literal.

2. **User data lives in the OS-standard location, never the working directory.** Resolve the database / config directory via Qt's `QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)` (→ `~/Library/Application Support/MFL` on macOS, `%APPDATA%\MFL` on Windows, `~/.local/share/MFL` on Linux — one call, no `if`). `QStandardPaths` is chosen over the `platformdirs` ADR-004 named because Qt is already a dependency and it needs no extra package. The `--db` override stays for development.

3. **Keyboard shortcuts: prefer `QKeySequence.StandardKey`; otherwise map the modifier per-OS.** Qt does **not** translate a literal `"Ctrl+N"` string to ⌘ on macOS — only `StandardKey` does. For actions with a standard key (Open, Save, New, Quit, Delete, Copy, Paste, Find…) use the `StandardKey` enum. For app-specific chords with no standard key, build the sequence through a single helper that emits the Command modifier on macOS and Control elsewhere, so menus read ⌘ natively on Mac and Ctrl on Windows.

4. **Menu roles for About / Preferences / Quit.** Any About, Preferences/Settings, or Quit action must set the appropriate `QAction.MenuRole` so Qt relocates it into the macOS application menu (where users expect it) while leaving it in the File/Help menus on Windows.

5. **Fonts: lead the cascade with the cross-platform system font.** No font stack may put a single-OS family first. Use `-apple-system` / `system-ui` ahead of `"Segoe UI"`, with neutral fallbacks (`Inter`, `Helvetica Neue`, `Arial`, `sans-serif`). Never hardcode a `QFont("Segoe UI")` (or any one-OS family) without a fallback.

6. **Style stays Fusion + QPalette (ADR-026).** Do not switch to a native platform style; Fusion is what gives identical metrics and honours the palette on every OS. Per-widget QSS must use hex colours and standard CSS properties only — no OS-specific assumptions.

7. **No OS-shell coupling.** No `os.startfile` (Windows-only), no `subprocess`/`QProcess` to a shell command, no platform-specific file-manager or "open in default app" calls. If "reveal in Finder/Explorer" or "open this file" is ever needed, route it through `QDesktopServices.openUrl` (cross-platform) and nothing else.

8. **Dates: no `%-d` / `%#d` / `%-m` directives.** Keep using the `f"{d.day} {d.strftime('%b %Y')}"` pattern. SQLite `strftime` is fine (it is the same engine everywhere).

9. **One codebase, no platform branches in feature code.** A `sys.platform` check is allowed *only* inside a small, isolated platform-shim helper (e.g. the shortcut-modifier helper in rule 3, or a console-encoding shim). Feature/UI/Repository code must never branch on the OS.

10. **Line endings: LF in the repo.** A `.gitattributes` enforcing LF (per ADR-004) must remain in place so files don't churn between machines.

### Release / packaging path

- **Build on each OS** — PyInstaller cannot cross-compile. macOS produces a `.app` (wrapped in a `.dmg`); Windows produces a `.exe`. The eventual clean answer is a **GitHub Actions matrix** (`macos-latest` + `windows-latest`) that builds both from one tag.
- **macOS distribution requires code-signing + notarization.** An unsigned `.app` triggers a Gatekeeper "unidentified developer" wall — unacceptable for the non-technical-friends audience. This needs an Apple Developer Program membership ($99/yr), a Developer ID certificate, and notarization via `notarytool`. This is the single largest *new* piece of work, and it is operational, not code. The packaging-tool choice (PyInstaller vs Briefcase/BeeWare, which automates dmg + signing + notarization) is deferred to the packaging round, not settled here.

---

## Consequences

### Positive
- The app runs on macOS today with no code change; "feature parity" is essentially free because the toolkit choices already did the work.
- A written, session-visible rule set turns portability from an accident into a maintained property — new features stay cross-platform by default.
- Replacing cwd-relative data storage with `QStandardPaths` (rule 2) is a prerequisite for shipping to non-technical users on either OS and dovetails with the deferred save/auto-save work (ADR-016 amendment).

### Negative / accepted trade-offs
- macOS code-signing/notarization carries an annual cost ($99 Apple Developer Program) and setup effort. Accepted: it is the only way to give friends a double-click-and-run experience on macOS.
- Builds must run on each platform (or in CI), so a release is two build jobs, not one.
- Hardcoded `Ctrl+` shortcuts and the Segoe-first font cascade are technically non-native on macOS *until* the Tier-1 work items below land; they are cosmetic (shortcuts still fire on the physical Control key), not blocking.

### Work items (close the current gaps)

These are the concrete deltas the audit surfaced; none is a blocker for *running* on macOS.

- **Tier 1 — native feel (small):**
  - Convert the 9 hardcoded `"Ctrl+…"` shortcuts in `register_window.py` to `StandardKey` where one exists, and route the rest (Ctrl+B, Ctrl+E, Ctrl+Alt+R, Ctrl+Shift+R) through the per-OS modifier helper (rule 3).
  - **Fix the `Ctrl+I` collision** — it is assigned to *both* Import (`register_window.py:521`) and Account Summary (`:560`). Platform-independent bug surfaced by the audit.
  - Reorder the font cascade in `theme.py:82` so `-apple-system` precedes `"Segoe UI"` (rule 5).
- **Tier 2 — data location (small, needs the save/auto-save decision):**
  - Move the default DB off cwd (`__main__.py:85` `DEFAULT_DB = Path("mfl_dev.db")`) onto `QStandardPaths.AppDataLocation` (rule 2). Settle alongside the ADR-016 save/auto-save amendment.
- **Tier 3 — packaging (its own round):**
  - PyInstaller/Briefcase config for `.app` + `.dmg` and `.exe`; Apple signing + notarization; optional GitHub Actions release matrix. Own ADR when started.

### Implementation notes (non-binding)
- The shortcut-modifier helper (rule 3) and the data-dir resolver (rule 2) are the two new shim points; keep each to a single small function so rule 9's "no platform branches in feature code" holds.
- ADR-004's `platformdirs` recommendation is superseded *for the desktop app* by `QStandardPaths` (no extra dependency); ADR-004 otherwise still applies.
