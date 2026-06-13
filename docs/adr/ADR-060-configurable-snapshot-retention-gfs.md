# ADR-060 — Configurable grandfather-father-son snapshot retention

**Date:** 2026-06-13
**Status:** Accepted
**Amends:** ADR-057 (automatic rotating snapshots — this replaces its flat `SNAPSHOT_KEEP = 10` retention and makes the cadence + tiers user-configurable, closing its §Ongoing-responsibilities "promote the constants to `setting` rows" item).
**Related:** ADR-035 (`setting` key/value table — where the policy is stored), ADR-059 (Data Library — the screen that now hosts the settings entry), ADR-050 (cross-platform rule set), ADR-009 (SQLite / WAL).

---

## Context

ADR-057 keeps the newest `SNAPSHOT_KEEP = 10` snapshots. Once the Data Library screen (ADR-059) made the `Snapshots/` folder visible, the owner asked to **let people configure snapshots**, worried that "after 6 months… there could be several GBs of snapshots."

The disk worry turns out to be inverted — the flat cap already bounds the folder at 10 copies, so it can't grow to GBs (10 × a few-MB SQLite file). The *real* defect the flat cap creates is the opposite: a single busy day fires the 30-min timer well past 10 times, so the keep-newest-10 window holds **only the last few hours** — and **every restore point older than this morning is already gone**. "My data as of last Saturday" (ADR-016's founding motivation) is unrecoverable after one active Tuesday.

The owner's own framing names the fix: *"30-min snapshots on the day, then daily for a week, then monthly only."* That's a classic **grandfather-father-son (GFS)** retention scheme — fine-grained recent history that thins as it ages — which keeps *deep* history (a month or two back) for a bounded, predictable footprint.

Two forks were resolved with the owner (`AskUserQuestion`):

1. **How much control** → *editable tier values* (spin boxes), not just presets — matching the existing currencies-dialog tunable pattern.
2. **Oldest tier** → *capped* (default 12 months), not kept-forever — so disk stays bounded.

## Options considered

### Retention model

- **GFS tiers (chosen):** keep *every* snapshot within `subdaily_hours` (today's 30-min captures), then *one per calendar day* back to `daily_days`, then *one per calendar month* back to `monthly_months`; older than that is deleted. Walking newest→oldest and keeping the first snapshot seen per day/month bucket yields the *newest* representative of each older period. Defaults `24h / 7d / 12mo`.
- *Keep-newest-N (status quo).* Rejected — the failure above; N has to be tiny to bound disk, which destroys older history, or huge to keep history, which bloats today.
- *Age cap only ("delete older than 90 days").* Rejected — still keeps every 30-min copy within the window (today's bloat unsolved) and offers no graceful thinning.

### What's configurable, and where it lives

- **Four knobs in the `setting` table, per file (chosen):** `snapshot_interval_min` / `snapshot_subdaily_hours` / `snapshot_daily_days` / `snapshot_monthly_months`. `setting` (ADR-035) already backs the FX tunables; a `RetentionPolicy` dataclass + `load_policy` / `save_policy` wrap it, with floor-clamping so a junk or zero stored value can never disable retention or zero the cadence.
- **Settings UI on the Snapshots tab of the Data Library screen (chosen)** — a `Settings…` button opening a small `SnapshotSettingsDialog` (four `QSpinBox`es + a live plain-English summary). The user is already looking at the snapshots there; no general Preferences dialog exists yet, and inventing one for four fields is overkill.
- *Per-file vs global config.* Per-file falls out for free (settings live in the `.mfl`, snapshots are per-file, the policy travels with a loaded working copy). A global default is a future nicety, not needed now.
- *Editable presets (Minimal/Balanced/Extensive).* Rejected per the owner's fork — raw tier values give full control and are no harder than the currencies dialog.

### Applying a changed policy

- **Save → persist → prune the existing set immediately → signal the window to re-arm the timer (chosen).** The effect is visible the moment the dialog closes (the list refreshes to the pruned set), and the new cadence takes effect without a restart. Pruning on *every* `maybe_snapshot` — including the no-change path — means an aging set keeps thinning (yesterday's sub-daily copies collapse to one daily) even on an idle day.

## Decision

- **`mfl_desktop/snapshots.py`:**
  - `RetentionPolicy` (frozen): `interval_min` / `subdaily_hours` / `daily_days` / `monthly_months`, with module-level `DEFAULT_*` (`30 / 24 / 7 / 12`).
  - `load_policy(repo)` / `save_policy(repo, policy)` over the four `setting` keys; `load_policy` clamps to floors (`interval ≥ 1`, `subdaily ≥ 1`, `daily ≥ 0`, `monthly ≥ 1`).
  - `prune(db_path, policy, now)` rewritten as the GFS bucketer (newest→oldest, keep-all / newest-per-day / newest-per-month / drop). Replaces `prune(db_path, keep)`. A new `_stamp_of` parses the filename timestamp (tolerating the `-N` collision suffix) so classification uses the embedded capture time, not mtime; files that don't parse as this db's snapshots are left untouched (never deleted).
  - `maybe_snapshot(repo, *, now=None, policy=None, force=False)` — `policy` defaults to `load_policy(repo)`; prunes on both the wrote-a-snapshot and the nothing-changed paths. `SNAPSHOT_KEEP` and `SNAPSHOT_INTERVAL_MIN` constants retired in favour of the policy + `DEFAULT_*`.
- **`mfl_desktop/ui/snapshot_settings_dialog.py`** — `SnapshotSettingsDialog`: four spin boxes seeded from `load_policy`, a live summary label, Save → `save_policy` + immediate `prune(now)` + `policy_saved` signal.
- **`mfl_desktop/ui/data_library_dialog.py`** — a `Settings…` button on the Snapshots tab; opening the settings dialog and, on `policy_saved`, refreshing the snapshot list and re-emitting as `settings_changed`.
- **`RegisterWindow`** — the launch timer interval now comes from `_apply_snapshot_interval()` (`load_policy(self._repo).interval_min`), called on launch, on `settings_changed`, and inside `_adopt_repository` (a loaded/opened file may carry a different cadence).

No schema migration (the `setting` table already exists); no new dependency; pure-stdlib date math (no `dateutil`).

## Consequences

### Positive
- **Deep history for a bounded footprint:** roughly *(today's 30-min captures) + 7 daily + 12 monthly* copies at steady state — "last Saturday" *and* "two months ago" both survive, which keep-newest-10 could not.
- Fully user-tunable per file, in the screen where snapshots are already shown; cadence changes apply live.
- Closes ADR-057's deferred "promote the constants to `setting` rows" follow-up.
- Pruning is idempotent and runs even on idle days, so an aging set self-thins.

### Negative / trade-offs
- The sub-daily tier still holds every capture within `subdaily_hours` — the heaviest tier, by design — so a very large DB edited all day briefly holds many copies before they collapse to one daily. Mitigated by lowering `subdaily_hours` or lengthening `interval_min`.
- Per-file config means the policy isn't shared across files; a user juggling many datasets (ADR-059) sets it per file. A global default is a future add.
- Retention is now an algorithm, not a count — `prune` must keep parsing timestamps from filenames, so the ADR-057 naming contract (`{stem}-YYYYMMDD-HHMMSS[-N]`) is load-bearing. `_stamp_of` returning `None` for non-matching files preserves the "never delete an unrelated file" guarantee.

### Ongoing responsibilities
- Any future change to the snapshot filename format must update `_stamp_of` in lockstep, or pruning silently stops classifying (and so stops deleting) older snapshots.
- If a global/default policy or a Preferences home is ever wanted, the `setting`-backed `RetentionPolicy` is the seam to lift.
