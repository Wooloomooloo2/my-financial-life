# ADR-098 — First-run onboarding (base currency + first account + import nudge) and an in-app Help menu

**Date:** 2026-06-21
**Status:** Accepted
**Related:** ADR-079 (version/About/licensing — the existing Help items), ADR-092 (launch file resolution / seeding), ADR-050 (file model — Snapshots/Library beside the live file), ADR-057 (snapshots + clean-close checkpoint), ADR-076 (theming), ADR-035 (`base_currency` setting), `RELEASE_1.0_BACKLOG.md` item **P5** + the K0 distribution decision.

---

## Context

P5 asks for "a short first-run flow (create/seed file, pick base currency, optional import nudge)," an in-app "Help / Getting Started," and an About box, plus a crash-safety review of the ADR-057 snapshot story under packaged file locations.

What already existed (audited): a brand-new file is **auto-seeded** on GUI launch (`__main__._seed_starter_db` → `mrl:Person_1` + a GBP "Current account"), and the app drops the user straight onto the Home dashboard. The dashboard cards self-hide / show muted "No X yet" placeholders. The About box (ADR-079) is complete (version from `version.py`, live license state, links). The Help menu had only the three ADR-079 items (About / Enter License / Buy).

The gaps were therefore narrow and specific:

1. **No base-currency choice.** `base_currency` was hardcoded to GBP at seed time, and the value the app actually reads is the `setting` table key `base_currency` (Home, Net Worth, sidebar, FX refresh all read it; the seed only stamped `person.base_currency`, leaving the setting unset → GBP fallback). A US/EU user had no way to set it short of editing the DB.
2. **No first-run guidance.** The empty dashboard was stable but silent — no welcome, no "import your first statement" path.
3. **No Help → docs link.** Nothing pointed at the (future) website docs.

---

## Decision

**(1) A one-time `FirstRunDialog` (`mfl_desktop/ui/first_run_dialog.py`).** Shown only when this launch actually seeded a brand-new file — `__main__` sets a `just_seeded` flag at the `_seed_starter_db` call site and opens the dialog over the freshly-shown register. It offers two fields:

- **Base currency** — an editable ISO-4217 typeahead (same pattern as `AccountDialog`), defaulting to the starter account's currency (GBP).
- **First account name** — defaults to the seeded "Current account".

Two exits, **both apply first**: **Get started** (close to the dashboard) and **Import a statement…** (close, then the caller opens the import picker on the starter account via the new public `RegisterWindow.start_first_run_import(iri)`). Esc applies nothing — the GBP defaults stand, exactly as before this dialog existed, so the flow is strictly additive and never a gate.

Apply is **best-effort per field** (a bad value never strands the user on a new file) and goes through the Repository:
- New `Repository.set_base_currency(code)` writes **both** the `setting` key (what the app reads) and `person.base_currency` (the seeded MRL-boundary value) in one transaction, so they can't disagree.
- The starter account is renamed and **re-currencied to the chosen base currency** via `update_account` — the common one-account-one-currency first case (a USD user shouldn't be left with a GBP "Current account").

After the dialog, `RegisterWindow.refresh_after_first_run()` re-renders the two currency-affected surfaces (Home display currency + sidebar balance labels).

**(2) Help → Getting Started / Visit Website.** Two new Help items open `version.DOCS_URL` / `version.WEBSITE_URL` in the browser, above a separator and the existing About / License / Buy items. The URLs are new constants in `version.py` (the product-constants home, beside `__version__`/`APP_NAME`), placeholders on the launch domain until the W-workstream site is live.

**(3) Crash-safety review — no change needed for 1.0.** K0 locked **direct signed+notarised distribution (non-sandboxed)** for 1.0. The ADR-057 model — `Snapshots/` and `Library/` written *beside* the live `.mfl`, plus the appdata default location — needs full file access, which direct-notarised has. The Mac App Store sandbox would break it, but that channel is explicitly deferred to K3/1.1+ (where security-scoped bookmarks + relocated container paths are already scoped). So the snapshot/checkpoint story holds as-is under the 1.0 packaging; this review's outcome is "confirmed, no code change," recorded here so it isn't re-litigated.

---

## Alternatives considered

- **A multi-step wizard** (welcome → currency → accounts → import). Rejected as too heavy for a local-first app whose seed already produces a working file; one compact dialog covers the real choices, and everything is editable later via the normal screens.
- **Track first-run with an app-level QSettings flag.** Rejected — a per-file signal is correct (a user can create several files), and the launcher already *knows* it just seeded. No persisted "onboarding done" flag is needed: the dialog only ever opens on the seed path.
- **Force the base currency before the window shows (modal pre-launch).** Rejected — showing the register first and layering the welcome on top means a closed/Esc'd dialog still lands on a usable app, and the dialog can read the seeded account to pre-fill.
- **Put the URLs in `license_service` beside `BUY_URL`.** Rejected — docs/website are product constants, not licensing; `version.py` is the Qt-free product-constants module. `BUY_URL` stays where its three callers already reference it.

---

## Consequences

- A brand-new file now opens with an intentional welcome that sets the base currency (closing the long-standing GBP-hardcode gap) and optionally walks the user straight into their first import — the empty-state concern folded in from P4 is addressed by giving the empty file a purpose, not just placeholder text.
- `set_base_currency` is a reusable single-write path; a future "change base currency" Settings affordance can call the same method.
- Help now links to Getting Started + the website; the placeholder URLs are the only thing gating that on the real W-workstream site.
- **Still open under P5** (not first-run): nothing — the in-app Help links + About + first-run flow are done. The website docs they point at are W2 (separate workstream).

---

## Verification

- `py_compile` clean on the five touched files; full app imports offscreen.
- First-run apply on a freshly-seeded DB: picking USD + renaming "Checking" persists `setting.base_currency = USD`, `person.base_currency = USD`, and the starter account → `Checking`/`USD`; `wants_import()` / `starter_account_iri()` return correctly.
- `RegisterWindow` builds against a seeded DB with the new Help items present (`Getting Started`, `Visit Website`, separator, About/License/Buy); `refresh_after_first_run()` and `start_first_run_import()` exist and run without error.
