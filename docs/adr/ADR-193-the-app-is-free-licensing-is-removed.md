# ADR-193 — The app is free: licensing and the trial are removed

**Date:** 2026-08-19
**Status:** Implemented
**Supersedes:** ADR-079 (offline license keys + the 30-day trial) and the licensing-dormancy half of ADR-125 (`is_store_build`).
**Related:** ADR-123 (the store-only 1.0.1 that had already put the key channel dormant), ADR-180 (the dev signing key rotation — now moot), `RELEASE_1.0_BACKLOG.md` workstream C.

## Context

ADR-079 built a one-time-purchase model: an Ed25519-signed offline license key verified on device, a 30-day full-feature trial recorded in `QSettings` (first-write-wins so a reinstall couldn't reset it), a launch nag when the trial ended, a title-bar countdown cue, an About-box state line, an Enter-License dialog, `cli license-check`, and `tools/license_tool.py` to mint keys. ADR-125 then added `is_store_build()` so all of it would go quiet in a Mac App Store build, where the store owns entitlement.

The owner's decision: **the app is free.** The README has said MIT since the start, and no key was ever issued (ADR-180 records the dev private key being lost in a machine move, with the observation that nothing had been minted). With no purchase, a trial clock is a countdown to a lockout nobody can lift.

Removing only the trial would leave the app in the worst state of the three: no trial and no key means `evaluate()` returns `EXPIRED`, i.e. locked. So "remove the trial" necessarily means "decide what licensing is for" — and the answer is nothing.

## Decision

**Delete the licensing subsystem entirely, rather than neutering it.**

Deleted: `mfl_desktop/licensing.py`, `mfl_desktop/license_service.py`, `mfl_desktop/ui/license_dialog.py`, `tools/license_tool.py`, `tests/test_store_build.py`.

Removed from the surfaces that used them:

- **Register window** — the launch nag (`_maybe_show_license_nag`), the title-bar trial cue (`_refresh_license_cue`, `_license_title_suffix`), the Enter-License handler, and the Help ▸ Enter License / Buy menu entries. The window title is now just file + view.
- **About box** — the license state line and the Enter-license / Buy buttons. It shows name, version, build revision (ADR-099) and publisher, and is now static: nothing to re-read, so `_refresh()` is gone with it.
- **`app_session`** — `get/set_license_key` and `get/set_trial_start`, and the two `QSettings` keys they wrote. A user who ran an earlier build keeps two orphan keys under `license/` in their settings; they are never read again. Not worth a migration to delete.
- **`version.py`** — `APP_EDITION` (the entitlement constant) and `is_store_build()`.
- **`cli.py`** — the `license-check` subcommand.
- **`stamp_build_info.py`** — the `--store` flag and the `STORE_BUILD` stamp, and the `--store` call in `build_mas.sh`.

**`is_store_build()` goes with it.** Its only reason to exist was making licensing dormant — its docstring said so, and every caller was a licensing surface. `sandbox.is_sandboxed()`, which it fell back to, is a real function about the App Sandbox and stays. If a store build ever needs behaviour of its own again, the stamp is four lines to reinstate.

## Rejected

- **Keep the key plumbing, drop only the trial** (app always unlocked, Enter License still available for a key nobody can mint). Dead UI that promises something the product no longer does, plus an Ed25519 verify path and a public key to keep explaining. The About box would still ask "Licensed to…?" of a free app.
- **Flag every build as a store build** (`is_store_build() -> True`). A one-line change and fully reversible — but it leaves the whole subsystem in the tree behind a lie, and About would read "Purchased via the App Store" on a Linux `.deb`.
- **Keep `licensing.py` as a library "for later".** Verify-only code with no caller is code that rots. Git has it; ADR-079 documents the format precisely enough to rebuild from.
- **Migrating away the orphan `QSettings` keys.** Two dead strings in a settings file, read by nothing. A migration to delete them is more code and more risk than the mess it tidies.

## Consequences

- The app is fully functional on first launch, forever, with no state that can ever lock it. There is no expiry path left to fail.
- The Help menu is Getting Started / Visit Website / About / Export Diagnostics. `BUY_URL` is gone; `WEBSITE_URL` and `DOCS_URL` (ADR-098) stay.
- **`cryptography` is no longer needed for licensing** — it is still a dependency for bank-feed credential encryption (ADR-077), so `requirements-desktop.txt` is unchanged.
- **The planning and marketing docs still describe a paid product.** `RELEASE_1.0_BACKLOG.md` workstream C and `WEBSITE_BRIEF.md` §7 (pricing, "try free for 30 days", key delivery) are now contradicted by the code; both carry a pointer to this ADR, but the pricing decision itself is the owner's to rewrite. **The website must stop advertising a trial and a purchase before this ships.**
- ADR-079, ADR-123 and ADR-125 stay on file as history; per the ADR convention they are not edited, and the index marks what this supersedes.

## Verification

- Import-all smoke over every `mfl_desktop` module: clean.
- Test suite: 72 of 73 pass (the one failure is `tests/test_budget_drilldown_editable.py`, which segfaults during GC under offscreen Qt identically on unmodified `master` — pre-existing, see ADR-192).
- No reference to `licens`, `trial`, `APP_EDITION`, `is_store_build`, `STORE_BUILD` or `BUY_URL` survives anywhere in `mfl_desktop/`, `tests/`, `tools/` or `packaging/` outside the two docstrings that explain the removal.
- The frozen Linux build launches, seeds a fresh database and opens the window with no licensing prompt, cue or nag at any point.
