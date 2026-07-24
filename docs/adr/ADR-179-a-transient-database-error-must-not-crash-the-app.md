# ADR-179 — A transient database error must not crash the app

**Date:** 2026-07-24
**Status:** Implemented
**Related:** ADR-156 (the freshness token and account-value memo this hardens). ADR-075 (Home refreshes on activation). ADR-063 (the Schedules cue refreshes on activation). ADR-099 (the crash handler that showed the dialog). ADR-035/044/116 (the background workers that open their own connections). ADR-057 (WAL mode, snapshots).

## Context

Owner-reported. My Financial Life, left open overnight, logged this three times in 44 seconds on the morning of 2026-07-24:

```
2026-07-24 07:50:46 CRITICAL mfl: Uncaught exception
Traceback (most recent call last):
  File "mfl_desktop/ui/register_window.py", line 608, in changeEvent
    self._home_view.refresh_if_stale()
  File "mfl_desktop/ui/home_view.py", line 321, in refresh_if_stale
    if self._freshness_token() == self._rendered_token:
  File "mfl_desktop/ui/home_view.py", line 303, in _freshness_token
    return (self._repo.data_generation(), date.today())
  File "mfl_desktop/db/repository.py", line 919, in data_generation
    self._conn.execute("PRAGMA data_version").fetchone()[0],
sqlite3.OperationalError: locking protocol
```

Four facts from the log, each of which shaped the decision:

1. **The failing statement is `PRAGMA data_version`.** SQLite's `SQLITE_PROTOCOL` — it could not settle the WAL-index (`-shm`) lock within its own retry budget. It is a *transient contention* result, not corruption: nothing here says the data is bad.
2. **The connection was not dead.** `_refresh_schedules_cue()` runs immediately before, on the same connection, and completed. So did every query in the twelve hours before. The failure was momentary and partial.
3. **The app had been idle for 12 hours** — the previous log line is 2026-07-23 19:46, and the machine slept in between. The trigger is environmental (a sleep/wake cycle, or another process disturbing the file locks); it is not reconstructible from the log and is not in our control.
4. **The crash repeated because the crash handler re-triggers it.** ADR-099's dialog is modal; dismissing it returns focus to the register window; that fires `ActivationChange`; which runs this handler; which crashes. Three crashes, 22 seconds apart, is one fault plus two dismissals.

What made a momentary lock blip fatal is *where* it was read. `data_generation()` is a **cache-invalidation hint** — its entire job is answering "is it worth redoing the work?" (ADR-156). It was being read from a **window-activation handler**, which the user triggers by alt-tabbing. Neither is a place worth dying in. An optimisation that cannot run should cost us the optimisation, not the session.

The pre-flight check that should have caught it does not. `refresh_if_stale` calls `Repository.is_open()` first, which probes with `SELECT 1` — a constant expression SQLite answers from the parser, touching no page, taking no WAL-index lock, and therefore incapable of observing the very condition that broke. It returned `True` for a connection that could not read a row.

A second gap surfaced while reading the connection setup: no `busy_timeout` was ever set, so it is SQLite's default of **zero**. Every connection in the app fails on the first contended lock instead of waiting — and the app runs several connections (the background price/FX workers open their own, ADR-035/044/116).

## Options considered

**A. Retry `PRAGMA data_version` and, failing that, degrade.** The hint reports "assume the data moved" when it cannot be read. Callers redo work they might not have needed to; nothing else changes.

**B. Reopen the connection on `SQLITE_PROTOCOL`.** Heavier and riskier: a `Repository` hands out no live cursors but does own the WAL, the snapshot timer, and `total_changes` — the generation counter that ADR-156's memo is keyed on. Silently swapping the connection under a running UI to paper over a blip that clears on its own is a bad trade.

**C. Make `is_open()` a real probe** (touch a page rather than `SELECT 1`). Tempting, and it *would* have caught this one. Rejected as the primary fix: it turns every ambient failure into a silent skipped refresh across all its callers, it makes a hot-path check do real I/O, and it is still a check-then-act race — the blip can land between the probe and the work. Its finding is worth keeping, though, so the reason `SELECT 1` cannot see this is now recorded in a test.

**D. Catch `Exception` at the top of `changeEvent`.** Too broad. A `TypeError` in a card builder is a bug that should be seen, not swallowed by the nearest event handler.

## Decision

**A transient database error in an ambient code path degrades; it does not raise.** Ambient means *the user did not ask for this* — a refresh triggered by focus landing on a window, or a probe deciding whether to skip work. A user-initiated action still fails loudly, with a real error, from a path the user can connect to what they did.

Three changes.

**1. `data_generation()` never raises.** The pragma moves into `_data_version()`, which retries up to 3 times at 25 ms (SQLite's own WAL-index backoff works on this timescale, and 75 ms worst case is imperceptible on the UI thread). If it still will not read, it logs a warning, returns `None`, **and bumps `_ext_gen`**.

That bump is the substance of the decision. It makes the token differ from the last one handed out, so every caller concludes *the data may have moved* and redoes its work. Failing stale-side is the only safe direction: the cost is a rebuild that might not have been needed — the app's behaviour before ADR-156 — whereas failing fresh-side would serve numbers from a cache we can no longer vouch for. Two consecutive degraded reads also differ from each other, so a persistent fault does not latch into a falsely-stable token.

**2. The activation path swallows `sqlite3.Error`.** `RegisterWindow.changeEvent` and `HomeView.refresh_if_stale` log and return. `sqlite3.Error` specifically, not `Exception` (option D).

**3. `PRAGMA busy_timeout = 5000` on every connection.** Unrelated to `SQLITE_PROTOCOL`, but it removes the adjacent class of transient failures — an immediate "database is locked" the first time a background worker's write overlaps a main-thread read. Five seconds is far longer than any write this app makes and short enough that a genuinely stuck lock is still an error rather than a hang.

## Consequences

**The root cause is not fixed, because it is not ours.** The `SQLITE_PROTOCOL` blip came from outside the app — a sleep/wake cycle, or another process touching the file locks — and will recur. What changes is the consequence: a warning line in the log and a dashboard that redraws, instead of a crash dialog that re-arms itself on dismissal. If it recurs *often*, the log now says so in a greppable form (`data_version unreadable`), which is the evidence a real root-cause investigation would need and did not have this time.

**Honest scoping of change 2.** It is defence in depth, not the fix. `refresh()` already swallowed a failed gather (`except Exception: return`, ADR-160); the hole was the token probe *in front* of it, which change 1 closes. The guards matter for the next database call added to an activation handler, and for the general rule that an event handler must not be able to take the app down. The tests pin this distinction rather than overstating it.

**The memo can now recompute needlessly.** A degraded token misses ADR-156's account-value cache, costing ~360 ms of FIFO replay. That is the correct trade at this frequency (once per blip) and is exactly what the app did before ADR-156.

**`is_open()` is unchanged and still cannot detect an unusable-but-open connection.** Left deliberately (option C), with `test_the_probe_that_should_have_caught_this_does_not` pinning why — so a future reader does not "simplify" the guards away in its favour.

## As built

- `mfl_desktop/db/repository.py` — `import logging`/`time`, module `logger`; `PRAGMA busy_timeout = 5000` in `__init__`; `data_generation()` delegates to the new `_data_version()`.
- `mfl_desktop/ui/home_view.py` — module `logger`; `refresh_if_stale` wrapped in `try/except sqlite3.Error`.
- `mfl_desktop/ui/register_window.py` — module `logger`; `changeEvent`'s activation branch wrapped in `try/except sqlite3.Error`.
- `tests/test_transient_db_error_does_not_crash.py` — 9 tests. The incident is reproduced with a connection wrapper that fails **only** `PRAGMA data_version` (the partial failure actually observed); a second wrapper passes `SELECT 1` and fails everything else, which is what exposed the `is_open()` blind spot.

Two of the first-draft tests passed vacuously — one because `_refresh_schedules_cue` has its own guard, one because a fully-dead connection is caught by `is_open()` — and were rewritten against the real failure shape. Recorded because it is the same mistake in miniature as the bug: a probe that cannot observe the condition it is standing in for.

Verified headless (`QT_QPA_PLATFORM=offscreen`), including the end-to-end case: a real `RegisterWindow`, confirmed active and showing Home, taking three `ActivationChange` events on a broken connection without raising. Full suite **432 passed, 0 failed**. No schema change.
