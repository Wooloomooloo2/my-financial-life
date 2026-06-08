# ADR-041 — Register date-window filter (default rolling quarter)

**Date:** 2026-06-08
**Status:** Accepted
**Related:** ADR-010 (transactional schema / Repository contract — this adds two windowed query variants), ADR-033 amendment (period-preset vocabulary — the register reuses a subset of the same finance-native presets). Distinct from the report/summary period selectors: this one governs how many register rows are *fetched and rendered*, not an aggregation range.

---

## Context

The owner reported a visible slowdown when displaying accounts with many transactions. Profiling the real `mfl_dev.db` (11,917 transactions; busiest account 6,403 rows) confirmed it and located the cost:

| Stage (busiest single account, 6,403 rows) | Time |
|---|---|
| `Repository.list_transactions_for_account` (SQL + build) | 69 ms |
| `model.reload` (begin/endResetModel) | 36 ms |
| Proxy sort by date | 199 ms |
| **Proxy sort by amount** | **513 ms** |
| Search filter (one keystroke) | 115 ms |
| Full `data()` sweep (all rows × all roles) | 2,314 ms |

All-transactions view (11,917 rows): load 112 ms + reset 103 ms + sort 583 ms.

The dominant interactive cost is the **proxy sort**. `QSortFilterProxyModel` invokes our Python `lessThan` once per comparison — ~80,000 boundary crossings for 6,403 rows — and it fires on every account switch and every column-header click. Sorting by Amount is half a second of UI freeze. The full `data()` sweep looks alarming but Qt virtualises painting (only the ~30 visible rows are queried per repaint), so it is *not* what the owner feels during normal scrolling; it would only bite if something forced a full sweep (e.g. a `ResizeToContents` header — the register deliberately uses `Interactive` with fixed widths, so it doesn't).

The fix is to stop materialising and sorting the entire history when the user almost always wants recent activity. A bounded default window collapses every line of that table for the common case.

---

## Options considered

### A — Proxy-level date filter (`filterAcceptsRow` drops out-of-window rows)

Add a date predicate to `TransactionFilterProxy`. Pros: no Repository change; the proxy only sorts the accepted subset, so the sort cost shrinks. Cons: the source model still loads **all** rows from SQL and still pays the full `model.reload` reset (the ~100–200 ms load+build+reset) on every account switch, because the window narrows only *after* the rows are in memory. It treats the symptom (sort) but leaves the load/reset cost intact, and it scales with total history rather than with what's shown. Rejected as the primary mechanism.

### B — Repository-level windowing (`WHERE posted_date >= ?` pushed into SQL)

Fetch only the windowed rows. Pros: cuts load, build, reset, **and** sort in one move — a 90-day view fetches a few hundred rows regardless of how many years the account holds, so the whole table above drops to single-digit milliseconds. Scales with what's shown, not with history. Cons: the running-balance column needs the balance *as of the window's first visible row*, which depends on every prior transaction. Solved with one extra aggregate (`SELECT SUM(amount) WHERE posted_date < window_start`) that seeds the running total before the windowed rows accumulate forward — O(1) query, fully indexed by date. **Selected.**

### C — Hybrid (Repository window for the default, proxy filter for live re-narrowing)

Both. Rejected as premature: Option B alone makes the default fast, and the window changes infrequently (a combo pick, not a keystroke), so paying one windowed re-query per change is fine. No need for the proxy to also carry a date predicate. Revisit only if a future "scrub the window with a slider" interaction appears.

### Default window — what the register opens on

Asked the owner directly. Options weighed: 30 days (fastest, risks feeling too short when scanning back), rolling quarter / 90 days (fast, covers day-to-day), YTD (variable — tiny in January, large by December), 12 months (more complete, slower on busy accounts). **Owner chose rolling quarter (90 days).** `All` remains in the selector for full history and reconciling, where the user accepts the cost knowingly.

---

## Decision

Add a **date-window selector** to the register filter bar and back it with windowed Repository queries.

**Presets** (matching the owner's stated list): `Last 30 days`, `Rolling quarter (90 days)`, `Year to date`, `All`. Default = **Rolling quarter**.

**Repository.** Both register query methods gain an optional `since: str | None = None` parameter (`'YYYY-MM-DD'`, inclusive lower bound; `None` = all history — unchanged behaviour):

- `list_transactions_for_account(account_id, since=None)` — when `since` is set, seeds the running balance with `opening_balance + SUM(amount) WHERE posted_date < since`, then selects and accumulates only `posted_date >= since`. The running balance of the first windowed row is therefore correct, not restarted from the opening balance.
- `list_all_transactions(since=None)` — adds `WHERE posted_date >= since`; no running balance in this view, so no seed query.

`posted_date` is stored as bare ISO `YYYY-MM-DD` (verified), so the bound is a plain lexicographic string comparison — no date parsing in SQL. The bound is **lower-only**: future-dated rows (scheduled/posted-ahead transactions, which exist in real data) stay visible, which is the desired behaviour for a register.

**Model.** `TransactionTableModel` carries `since` (constructor arg + `set_since(since)` which resets and reloads). `reload()` passes it through to the Repository.

**Window UI.** `RegisterWindow` holds the current window key. `_show_account` / `_show_all_transactions` build the model with the current `since`; the combo handler calls `set_since` to re-window the existing model in place (no delegate teardown — the column layout is unchanged). The window applies to both single-account and all-transactions views. The selection is **session state, not persisted** — a per-account saved preference is already a separate backlog item and is deliberately out of scope here.

`since` is computed from `date.today()`: 30/90-day presets subtract days; YTD is `date(today.year, 1, 1)`; All is `None`.

---

## Consequences

### Positive

- **The common case is effectively instant.** A 90-day default fetches and sorts a few hundred rows instead of thousands; the 200–500 ms sort freeze on account switch and column-sort disappears for the default view.
- **Scales with what's shown, not with history.** A 19-year account costs the same to open as a 1-year account at the same window.
- **Running balance stays correct inside a window** via the seed-sum, so the windowed view isn't a lie — the Balance column matches what All would show for the same rows.
- **Future-dated rows remain visible** (lower-bound-only), so scheduled/posted-ahead transactions don't vanish from the default view.

### Negative / trade-offs

- **`All` is still slow on huge accounts** — the sort cost is inherent to a Python `lessThan` over thousands of rows. This ADR makes All opt-in rather than the unavoidable default; a deeper fix (a C++-side sort key, or caching a sort tuple per row) is a separate optimisation if All-view performance becomes a real complaint.
- **Status bar "Showing X of Y"** now reports the *windowed* total as Y, not the account's lifetime count. Acceptable — the selector communicates the active window — but worth a glance if the count ever needs to mean "everything".
- **The window is global, not per-account.** Switching accounts keeps the chosen window. A per-account saved preference (backlog) would change this; intentionally not built here.

### Ongoing responsibilities

- **Any new register query path must thread `since` through** or it silently reverts to loading all history. The two existing methods are the only register feeds today.
- **The seed-balance query and the windowed select must use the same `since` bound** (`< since` for the seed, `>= since` for the rows) or the running balance will double-count or skip the boundary day. The comment in `list_transactions_for_account` records this.
