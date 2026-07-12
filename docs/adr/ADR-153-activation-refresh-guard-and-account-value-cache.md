# ADR-153 — Window activation stops rebuilding Home; account values are cached

**Date:** 2026-07-12
**Status:** Implemented
**Related:** ADR-063 (Schedules cue refreshed on window activation). ADR-075 (Home dashboard as stack page 0, refreshed on activation). ADR-149 (Home refresh defers widget destruction — the previous bug on this same code path). ADR-150 (heavy Home cards computed off-thread). ADR-044 (investment market value). ADR-035 (launch FX refresh). ADR-116 (toolbar market refresh). ADR-121 (historical net worth).

## Context

Owner report, testing on Windows: the app "is starting to feel sluggish." Launching a report takes about a second. The front end "sticks" just after closing a report, or when switching from a report to a register. Not terrible, but noticeably worse than on a MacBook Pro that is only slightly more powerful — and this is a lightweight app on a 64GB Zen 5 machine. Something was wrong, and it was not the hardware.

Measured against the live file (`mfl_dev.mfl`: **35,054 transactions, 26 accounts, 200 categories, 3,135 payees** — small):

| Operation | Cost | How often it runs |
|---|---|---|
| `HomeView.refresh()` | **472–553 ms** | **every window activation** |
| └ `Repository.compute_account_values()` | **362 ms** (77% of it) | inside the above |
| `Repository.list_category_tree()` | 50–75 ms | **twice** per report open |
| `SpendingReportWindow.__init__` | ~150 ms | per report open |

### The freeze: activation rebuilds the whole dashboard

`RegisterWindow.changeEvent` refreshed Home whenever the window regained activation and Home was the visible page:

```python
if event.type() == QEvent.ActivationChange and self.isActiveWindow():
    self._refresh_schedules_cue()
    stack = getattr(self, "_main_stack", None)
    if stack is not None and stack.currentIndex() == 0:
        self._home_view.refresh()          # ~450-550ms, synchronous, on the UI thread
```

The docstring justified this with *"Cheap enough to run on every activation (a handful of schedules)"* — which was true of `_refresh_schedules_cue` (0.1ms), the thing it was originally written for. `HomeView.refresh()` was added later (ADR-075) under the same sentence, and it is not cheap: it re-derives every account value and reconstructs the entire card widget tree.

`ActivationChange` fires on **every** return of focus. Closing a report hands activation back to the register window. So does clicking from a report onto a register. So does alt-tab. Each one paid ~450ms, and in the overwhelmingly common case redrew a dashboard **identical** to the one already on screen.

That single fact explains all three reported symptoms:

- **"Launching a report takes about a second."** Clicking a report in the sidebar activates the register window *first* (450ms Home refresh), and only then constructs the report (~150ms). The report itself is the smaller half of the wait.
- **"Sticks just after closing a report."** Close → activation returns → 450ms rebuild.
- **"Sticks switching from a report to a register."** Activation (450ms) *plus* a full `TransactionTableModel` rebuild and delegate teardown/recreate.

Worth recording what it was **not**. Report modules are imported at the *top level* of `register_window.py`, so their cold-import cost (400ms–2.3s on Windows — startup is where that is paid) does **not** land on the click. And SQLite is not the problem: 35k rows is nothing, and the queries themselves are milliseconds. The cost is Python-level work on the UI thread, recomputed on an event that fires constantly.

### Why `compute_account_values` costs 362ms

It caches nothing. For each of the 7 investment accounts it pulls the account's **entire transaction history**, with no date bound, and replays it through the FIFO holdings engine from scratch (`repository.py`, `compute_holdings_view`) — ~24,000 `pence_to_decimal` calls per refresh. The answer is the same every time until someone writes to the database, and it is recomputed on every focus change, every sidebar reload, and every Home refresh.

### Why Windows feels worse than the Mac

Unproven, and recorded as the leading hypothesis rather than a finding: the same ~450ms runs on both machines. But every report is a separate top-level `QMainWindow` (reports are not pages in the existing `QStackedWidget`), so the app leans hard on window-activation events — and Windows fires `ActivationChange` far more eagerly when juggling several top-level windows than macOS does, where app-level activation absorbs much of it. Same bug on both; Windows just pulls the trigger more often. The fix below is deliberately independent of that hypothesis: it removes the cost rather than the events.

### A latent crash, found while profiling

Every profiling run of `HomeView` ended with a hard error on a worker thread:

```
Error calling Python override of QRunnable::run():
  File "mfl_desktop/ui/home_view.py", line 216, in run
    self._signals.done.emit(...)
RuntimeError: Signal source has been deleted
```

`_HomeBgRunnable` (ADR-150) borrowed the *view's* signals object (`HomeView._bg_signals`) instead of owning one, unlike `_MarketRefreshRunnable`, which owns its own. But **ownership turned out not to be the trigger**: reproduced both ways, destroying the view mid-pass is harmless, because the runnable holds a strong reference to the borrowed object.

The actual trigger is **app shutdown**. When the user quits with a Home background pass still in flight, Qt/PySide destroy the C++ QObject regardless of which Python object holds a reference to it, and the worker then emits from a deleted sender. Ownership is worth fixing anyway (it decouples the worker from the view's lifetime, and matches the pattern already used in `register_window.py`) — but it is *not* the fix. The fix is to guard the emit.

This is very likely why the live file has been unusable in the screenshot harness.

## Decision

**Three changes. None of them make the app do less; they make it stop doing the same work over and over.**

### 1. A data-generation token, and an activation refresh that checks it

`Repository.data_generation()` returns a cheap token that changes whenever the data behind a derived value may have moved. It combines three sources, because no single one sees every writer:

- **`sqlite3.Connection.total_changes`** — rows written through *this* connection. A plain attribute (~90ns, no SQL). Covers every edit made through the UI.
- **`PRAGMA data_version`** (~12µs) — commits made by another *process*.
- **`_ext_gen`**, bumped by hand via `note_external_change()` — for the background threads that write through their **own** `Repository` on their own connection (the launch and toolbar price/FX refreshes). Neither of the above reliably sees those.

On that last point, honestly: `PRAGMA data_version` is *documented* to observe other connections' commits, and in one probe it observed a sibling in-process connection — but in another configuration it did not. Rather than depend on a behaviour I could not make deterministic, **`note_external_change()` is the contract** and `data_version` is a cheap backstop that also happens to catch other-process writers.

`HomeView.refresh_if_stale()` compares `(data_generation(), date.today())` against what the currently-rendered dashboard was built from, and rebuilds only on a difference. `RegisterWindow.changeEvent` calls that instead of `refresh()`. The ADR-075 guarantee is preserved exactly — a real edit anywhere bumps the generation, so it still redraws — while the common no-op case drops from ~450ms to **~0.016ms**. `date.today()` is in the token so the date-relative cards (bills due, "last 30 days") still roll over on an app left open past midnight, which is the same concern ADR-063 has for the Schedules cue.

The token is sampled *before* the rebuild, not after, so a background write that lands mid-rebuild leaves it stale rather than being wrongly recorded as already on screen.

### 2. `compute_account_values` memoised against the generation

Keyed on `(generation, include_closed, as_of_date)` — the arguments change the answer, so they are part of the key. Callers get a **copy**, since they treat the returned dict as theirs to mutate and `Decimal` values are immutable, so a shallow copy fully isolates them. First call ~180ms; subsequent calls **~0.009ms**, until a write invalidates.

### 3. The background emit is guarded

`_HomeBgRunnable` now owns its signals object (matching `_MarketRefreshRunnable`), *and* every emit goes through `_emit()`, which checks `shiboken6.isValid(self.signals)` before emitting and swallows the `RuntimeError` if shutdown wins the race between the check and the emit. Dropping the payload is exactly right — the receiver is gone too.

The launch refreshes (`__main__`) gained the same guard, plus a completion signal: they now report whether they actually **wrote** anything (via a new `Repository.total_writes`), and only then ask the main thread to invalidate and redraw. Before this ADR they were pure fire-and-forget, and their new prices appeared on screen *by luck* — the next activation rebuilt Home from scratch regardless. Once derived values are cached, luck is no longer good enough: a writer on another connection has to say so, or the user would keep seeing pre-refresh figures until their next edit.

### Rejected

- **Cache report windows and reuse them.** Treats the smaller half of the report-open cost (~150ms of ~600ms) and introduces a stale-window problem, while leaving the 450ms activation rebuild — the actual freeze — untouched.
- **Move `gather_home_data` off the UI thread wholesale.** Hides the cost instead of removing it, and would make Home flicker or lag behind an edit. The work is redundant, not merely slow; deleting redundant work beats parallelising it. (The genuinely heavy cards are *already* off-thread — ADR-150.)
- **Debounce/coalesce the activation refresh on a timer.** Makes the freeze land slightly later rather than not at all, and adds a visible lag to legitimate refreshes.
- **Invalidate the cache from inside every mutating repository method.** `repository.py` is 10k lines with dozens of write paths; a single missed one is a silently stale balance. `total_changes` is maintained by SQLite itself and cannot be forgotten.
- **Drain the thread pool on quit** to fix the shutdown race. Correct, but it would block the UI on exit for the length of a full net-worth trend computation. Guarding the emit costs nothing.
- **Rewrite `list_category_tree` in this ADR.** Deferred deliberately — see below.

## Consequences

- Closing a report, and switching from a report to a register, no longer freeze. The activation path went from a full dashboard rebuild to a token comparison: **~450–550ms → ~0.016ms** when nothing has changed.
- Opening a report is now dominated by the report's own construction (~150ms) instead of an unrelated dashboard rebuild that ran first.
- Every consumer of `compute_account_values` gets the win, not just Home — the sidebar balances (`_sidebar_balances` → `compute_account_values(include_closed=True)`) and Net Worth were paying the same 362ms.
- Quitting the app while a Home background pass is in flight is now silent. `tests/test_activation_refresh_and_value_cache.py` pins it in a subprocess, judged on stderr, because the failure surfaces on a worker thread where pytest cannot see it.
- **New invariant, and the thing to remember:** *anything that writes through its own `Repository` connection must call `note_external_change()` on the main thread when it lands.* Two writers exist today (the launch refresh in `__main__`, the toolbar refresh in `register_window`) and both do. A third that forgets would show stale balances — which is exactly the failure mode the old always-rebuild architecture was accidentally immune to. This is the price of the cache, and it is stated here because it will not be obvious to the next reader.
- The generation counter is per-`Repository`, so `File ▸ Open` (which swaps the repo) correctly forces a rebuild: `HomeView.set_repo` drops the token.
- 10 new tests; 7 of them fail against the pre-ADR code. Full suite 239/239 (twice). No schema change.
- **Two pre-existing flaky tests in `test_home_refresh_use_after_free.py` (ADR-149) were fixed on the way**, having been exposed — not caused — by the extra CPU load of the new tests. Verified flaky against the *unmodified* source under artificial load, so neither was a regression from this ADR.
  - `test_real_home_refresh_survives_a_click_that_rebuilds_it` iterated a **snapshot** of the Home cards, but a click that rebuilds Home destroys the rest of them (and so does the ADR-150 background pass when it lands mid-loop). The next click then hit a freed Python wrapper and raised `RuntimeError`. It now re-queries the live cards before every click. That is a bug in the test, not the use-after-free it exists to pin.
  - `test_deleting_the_clicked_card_synchronously_would_crash` asserted that the raw unguarded pattern segfaults — but a use-after-free is *undefined behaviour*, not a guaranteed fault, and under load the freed memory was sometimes still mapped and the click unwound cleanly. It now retries 5 times and asserts it crashes **at least once**, which is the honest form of the claim: were Qt ever to genuinely make the pattern safe, all five would survive and the canary would still fire.

### Known, deliberately left

- **`list_category_tree()` is 50–75ms and every report constructor calls it twice.** The usage count is a correlated subquery that re-scans the split-unrolled `txn_category_line` view **once per category** — 200 full passes over 35k rows. `EXPLAIN QUERY PLAN` confirms `CORRELATED SCALAR SUBQUERY`. Rewriting it as a single `GROUP BY` + `LEFT JOIN` returns byte-identical results in half the time, and the double call per report is pure waste. Left out of this ADR to keep it to the freeze; it is the obvious next cut and worth ~100ms off every report open.
- **`_on_bg_ready` calls `refresh()` again** when the background cards land, so one activation can still rebuild Home twice. Now much cheaper (the second pass hits the value cache), but the redundancy stands.
- **`AccountSummaryWindow`, `BudgetWindow` and `TransactionsListWindow` each reload on every `WindowActivate`** — the same anti-pattern this ADR fixes for Home, on three more windows. They should grow the same freshness guard. Not done here because none of them was implicated in the reported symptom.
