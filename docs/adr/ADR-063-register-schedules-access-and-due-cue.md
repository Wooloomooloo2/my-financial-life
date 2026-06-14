# ADR-063 — Schedules access from the register + overdue/due-soon cue

**Date:** 2026-06-14
**Status:** Accepted
**Related:** ADR-023 (scheduled transactions — the `scheduled_txn` model and the `SchedulesDialog` this surfaces; the launch-time `auto_post_due` sweep). ADR-027 (Create Schedule From Transaction — the existing register→schedule right-click verb). ADR-033 (`account_summary.py` pure-helper home + the per-account "Upcoming" block / `upcoming_scheduled`). ADR-062 (the filter bar this button joins; the "● active" decoration idiom). Closes **A6**, the final item of the "Arc A — register tidy-up" cluster (A1–A5 already shipped).

---

## Context

Schedules (recurring bills, income, transfers) were reachable only from **Manage ▸ Schedules…** — a menu two levels deep — or indirectly via the per-account summary's "Upcoming" block. The register, where the owner spends most of their time, had **no direct way in** and, more importantly, **no ambient signal that a bill was overdue or about to fall due**. A scheduled bill could slip past unnoticed until the owner happened to open the management dialog.

Arc A's brief for A6 (owner braindump): *"schedules/bills access from the register + due≤3d/overdue cue."* Two pieces:

1. **Access** — a one-click way to open the schedules list from the register.
2. **A cue** — a passive indicator on that affordance flagging anything overdue or due within 3 days, so bills surface without the owner going looking.

Two genuinely user-facing choices were put to the owner via `AskUserQuestion`:

1. **Button label + cue shape** → **"Schedules" with a coloured count badge** (keeps the app's existing *Schedules* vocabulary — matches Manage ▸ Schedules — over the alternative "Bills"; a numeric badge over a bare dot so the count is visible without opening the dialog).
2. **Do auto-posting schedules count toward the cue?** → **Yes, count everything.** The cue mirrors the `SchedulesDialog`'s own "N due in the next 30 days" summary, which has never distinguished auto-post. (The considered alternative — excluding auto-posters as "needs no action" — was rejected as a surprising divergence from the dialog the button opens.)

The "due soon" horizon is fixed by the brief at **3 days**.

---

## Decision

Add a **"Schedules" button** to the register filter bar, between **＋ New Transaction** and **Reconcile…**. Clicking it opens the **same `SchedulesDialog`** as Manage ▸ Schedules (it reuses `_on_manage_schedules`, so Post Now / edits refresh the register + sidebar via the existing `schedules_changed` path — no new wiring).

The button label carries the cue, computed by a new pure helper and applied by `RegisterWindow._refresh_schedules_cue()`:

- **Nothing pending** → plain `Schedules` (no colour).
- **Only due within 3 days** (none overdue) → `Schedules ● N` in **amber** (`#b45309`), `N` = count due in `[today, today+3]` inclusive.
- **Anything overdue** → `⚠ Schedules (N)` in **red** (`#b91c1c`), `N` = **overdue + due-soon total**, so the figure matches what the dialog shows on click.

The tooltip always spells out the split (`"2 overdue · 1 due within 3 days"`).

**Pure core (`mfl_desktop/account_summary.py`).** New frozen `BillsDueSummary(overdue, due_soon)` (+ `total` / `has_alert` properties) and `bills_due_summary(schedules, today, horizon_days=3)`. It buckets active schedules: `next_due_date < today` → overdue; `today ≤ next_due_date ≤ today+horizon` → due-soon. `today` is injected so the function is pure and verifiable offscreen; `horizon_days` is a parameter (A6 fixes it at 3) so the value lives in one place. **Auto-post schedules are counted** like any other (owner decision above). Computed over **all** active schedules across every account — the cue answers "is anything I scheduled overdue or about to come due?", independent of which account is in view, consistent with the cross-account dialog it opens.

**Refresh points.** `_refresh_schedules_cue()` runs: at the end of `__init__` **after** `_run_auto_post_sweep()` (so an auto-poster the launch sweep just advanced doesn't briefly flash overdue); in `_on_schedules_changed` (after Post Now / edit / delete); and from a new `changeEvent` override on `QEvent.ActivationChange` when the window becomes active (the due/overdue split is date-relative, so an app left open across midnight — or a schedule posted from another window — re-colours on focus without a relaunch). The query is a handful of rows, so re-running on activation is free; it never caches and so can't go stale.

**No Repository, schema, or model change** — `list_scheduled_txns()` already exists; the cue is view-layer only. Best-effort: a DB error in the cue resets the button to its plain label rather than trapping the user.

---

## Options considered

- **"Schedules" label (chosen)** vs. "Bills". "Bills" reads more naturally for the overdue case, but the app already says *Schedules* everywhere (the menu, the dialog title, `scheduled_txn`); a second name for the same thing invites confusion. Owner picked consistency.
- **Count badge (chosen)** vs. a bare coloured dot vs. no cue. The dot is cleanest but hides the count until you open the dialog; the badge costs a little width but tells you *how many* at a glance. Owner picked the badge.
- **Count everything (chosen)** vs. exclude auto-posters. Excluding auto-posters gives a purer "needs your action" semantic, but diverges from the dialog's own summary and from what the user sees on opening it. Rejected as surprising; revisit only if auto-posters left open across a session prove noisy (they're materialised at next launch regardless).
- **Reuse `SchedulesDialog` (chosen)** vs. a register-embedded schedules panel. An inline panel would be a much bigger build for A6's "access" brief; the existing modal already does everything and stays the single source of truth. The embedded view, if ever wanted, is a separate arc.
- **Cross-account cue (chosen)** vs. a per-account cue scoped to the in-view account. Per-account would be more "local", but the button opens the cross-account dialog, so a global count is the consistent and more useful "don't miss any bill" signal. A per-account variant can layer on later if asked.
- **Refresh on activation (chosen)** vs. launch + mutations only. Adding `changeEvent` is a tiny, idiomatic override (other secondary windows already refresh on `WindowActivate`) and fixes the open-overnight staleness for free.

---

## Consequences

### Positive
- Bills surface **passively** on the screen the owner already lives on — the core "don't miss a bill" win — with one click to act.
- Zero schema/Repository/model change; pure helper is fully unit-testable offscreen.
- The cue figure reconciles with the dialog it opens (overdue + due-soon total), so the number never feels inconsistent.

### Negative / trade-offs
- **Counting auto-posters can over-state "needs action"** — an auto-paid bill due tomorrow adds to the count even though the owner needn't lift a finger. Accepted per the owner decision (consistency with the dialog); flagged here in case it grates.
- **The cue is launch/activation/mutation-driven, not a live clock** — a bill that crosses into "overdue" while the window sits focused won't re-colour until the next activation or schedule change. Acceptable: the practical trigger (open the laptop, switch back to the app) covers the real case, and a per-minute timer would be churn for a once-a-day event.
- **One more button on an already-busy filter bar.** Mitigated by Arc A having *removed* the Category combo (A2); the bar still has room. Future additions should weigh the popover (ADR-062) instead.

### Ongoing responsibilities
- **Any new schedule-mutating path must call `_refresh_schedules_cue()`** (or route through `_on_schedules_changed`), or the cue can lag a change made outside the dialog.
- **The 3-day horizon lives in `bills_due_summary`'s default** — change it there, not at call sites, if the owner ever wants a different lookahead (or a Preferences knob).
- **The auto-post-vs-cue decision is recorded here** — revisit this ADR, don't silently flip the helper, if the over-statement trade-off needs changing.
