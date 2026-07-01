# ADR-127 — Lockable API-key field: read-only until "Change"

**Date:** 2026-07-01
**Status:** Implemented
**Related:** ADR-044 (Securities dialog — Tiingo key). ADR-035 (Currencies dialog — OpenExchangeRates key; the twin screen). ADR-076 (semantic light/dark tokens — the greyed state is token-driven). ADR-096 (`transfer_chips` — precedent for extracting a shared UI helper used by two dialogs). ADR-126 (the TLS fix that made these keys work in the frozen app — the same session).

## Context

Both provider screens put the API key in an always-editable `QLineEdit` pinned at the **top** of the screen: Securities → the Tiingo token (`securities_dialog.py`), Currencies → the OpenExchangeRates `app_id` (`currencies_dialog.py`). Once a key is entered and saved, it re-loads into that same editable field every time the dialog opens. Owner report: with the key sitting right at the top, editable, it "looks too easy to accidentally edit" — a stray click-and-type or a select-all-delete silently corrupts a working key, and because the field is a password field the damage isn't obvious.

## Decision

A stored key opens **locked**: read-only and visually greyed, with a small **Change** button beside it that unlocks it for editing. A fresh (empty) field opens **unlocked** so a first-time user can paste straight in with no extra click.

Implemented as a shared component `mfl_desktop/ui/secret_field.py` — `LockableSecretField`, a `QWidget` wrapping a password `QLineEdit` + a Change button — rather than duplicating the lock logic in both dialogs (mirrors ADR-096's `transfer_chips` extraction). Behaviour:

- Constructed with the stored `value`; **locks automatically iff that value is non-empty**.
- **Locked** → `setReadOnly(True)` (not `setDisabled` — read-only still allows select/copy and keeps normal cursor semantics), plus a token-driven greyed style (`surface_alt` fill + `muted_strong` text + `border`) so it reads as non-editable in both themes (ADR-076); the Change button is shown.
- **Change** → unlock: restore the global QSS look, focus the field, and select-all so a replacement paste overwrites cleanly; the Change button hides.
- `.text()` / `.setText()` are proxied to the inner line edit, and each dialog keeps `self._key_edit = self._key_field.line_edit`, so the existing refresh/backfill/save handlers read the value **unchanged** — a locked field still submits the stored key on Refresh/Save.
- The Change button is `autoDefault(False)` so it never steals Enter from the dialog's default (Save / Refresh) button.

Rejected: `setEnabled(False)` for the locked look (dims too hard, blocks select/copy, and greys the label too); a per-dialog checkbox "Edit key" (more chrome than a single Change button, and duplicated in two places); re-locking on every keystroke or on Refresh (surprising — once the user chooses to change it, keep it editable until the dialog closes; it re-locks naturally on the next open).

## Consequences

- A stored Tiingo / OXR key can no longer be edited by accident — it takes a deliberate Change click. View/dialog layer only; no migration; no change to how keys are stored (`setting.tiingo_api_key` / `oxr_api_key`) or read.
- One new shared widget reused by both provider dialogs; the two `QLineEdit` construction blocks collapse to a `LockableSecretField` each.
- New `tests/test_secret_field_smoke.py` (8/8, offscreen) pins the contract: seeded → locked, empty → unlocked, Change unlocks and preserves the value, and both dialogs lock a stored key while exposing it via `_key_edit`. Verified visually in light and dark mode.
- Future secret fields (a hosted-feed token, an Enable Banking app key) can reuse `LockableSecretField` for the same protection.
