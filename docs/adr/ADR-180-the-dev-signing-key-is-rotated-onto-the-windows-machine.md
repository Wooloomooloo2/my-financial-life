# ADR-180 — The dev signing key is rotated onto the Windows machine

**Date:** 2026-07-24
**Status:** Implemented
**Related:** ADR-079 (the licensing scheme this rotates a key for; it anticipated exactly this swap). ADR-123 (Store-only 1.0.1 — why no issued keys are at risk). ADR-125 (`is_store_build()`, the other way the trial goes dormant). ADR-109 (`app_session`/QSettings, where the key and trial clock live).

## Context

Owner-reported: the app launched with **"Trial (1d left)"** in the title bar. Confirmed from `QSettings` — `license/trial_start = 2026-06-25`, `license/key = None`, `TRIAL_DAYS = 30` — so the owner's own build was due to hit `STATE_EXPIRED` on 2026-07-25.

The scheme (ADR-079) is working exactly as designed. The problem is custody. Minting a key needs the Ed25519 **private** half of the shipped `LICENSE_PUBLIC_KEY_B64`, which lives only in `tools/.dev_signing_key` — gitignored, deliberately never committed, and therefore **left behind on the Mac** in the Mac→Windows move. A search of the Windows machine found no copy under any of the ignored names. So the one machine now doing the development could not issue itself a license, and the trial clock is `first-write-wins` by design (`ensure_trial_started`), so it cannot be honestly reset either.

Worth stating plainly: the trial is *the owner's own* enforcement mechanism, on the owner's own build, and the offline-key path is the intended way to unlock it. This is a custody problem, not a licensing-policy question.

## Options considered

**A. Rotate the dev keypair and self-issue a license.** `keygen` a fresh pair on this machine, swap `LICENSE_PUBLIC_KEY_B64`, mint an edition-1 key, install it through `license_service.apply_license_key`.

**B. Reset `license/trial_start` in QSettings.** Buys 30 days, then recurs, and defeats the first-write-wins property the code deliberately has. A hack that has to be repeated.

**C. Raise `TRIAL_DAYS`.** Changes shipped behaviour for every user to solve one machine's custody problem. Wrong lever.

**D. Stamp `STORE_BUILD = True` into the local `_build_info.py`.** Makes `current_status()` short-circuit to owned (ADR-125). But it *mislabels a source checkout as a store build*, and it would silence the licensing UI in the very environment where that UI needs testing.

**A**, because it uses the mechanism as intended and is permanent. ADR-079 already names this exact move — *"a **development** key … replace it with the production public key"* — so rotating a dev key is routine maintenance, not a new design.

## Decision

**Rotate the development signing key onto the machine that does the development, and issue the owner a perpetual edition-1 license through the app's own API.**

- New keypair generated with `tools/license_tool.py keygen`; private half at `tools/.dev_signing_key` (already gitignored via both `tools/.dev_signing_key` and `*.signing_key`).
- `LICENSE_PUBLIC_KEY_B64` → `hNCRKH7EE/mUAPUTTkTcaK/HtKgocp4rnLPDjlOhtGg=`, with a comment recording the rotation and why.
- Key minted for `Mark Hall <mark.a.hall@gmail.com>`, edition 1, issued 2026-07-24, and installed via `license_service.apply_license_key` — the real path, so it was signature-verified and edition-checked before being persisted, exactly as a pasted key would be.

**Keys signed by the old private key no longer verify.** That is safe here and only here: the offline-key channel has never issued a key. ADR-123 made 1.0.1 Store-only, where the store owns entitlement and `is_store_build()` keeps this machinery dormant, so the only key in existence is the one minted above. Rotating after any key is issued to a real buyer would be a breaking change requiring reissue.

## Consequences

**The trial is gone for good on this machine, not deferred.** `current_status()` returns `state=licensed`, `unlocked=True`, `"Licensed to Mark Hall"`; the ADR-079 title-bar cue and launch nag go quiet because they all read that one chokepoint. A license carries no expiry — only an `ed` entitlement — so this does not recur, and it survives reinstalls because the key is in `QSettings`, not the data file.

**The custody problem is fixed, not solved.** The new private key is again a single gitignored file on a single machine — the same fragility that caused this, now pointed at Windows instead of the Mac. It is *not* backed up by the repo, by design. **The owner should copy `tools/.dev_signing_key` somewhere durable and offline** (password manager or equivalent). If it is lost again the fix is another rotation, which stays cheap only while no keys are in the wild.

**Nothing about production changes.** The shipped key is still a *dev* key with a dev private half; ADR-079's requirement to swap in a production key — private half held offline or by the MoR — before any paid non-Store build is untouched. Under ADR-123 that day may not come, since the Store owns entitlement.

**Verified:** the suite is green (**432 passed, 0 failed** — no test hardcodes a signed key or the public key, which is what made the rotation cheap), a real `RegisterWindow` built offscreen now reports `_license_title_suffix == ''` with no "Trial" in the title, and `git check-ignore` confirms the private key is ignored and unstaged. No schema change.
