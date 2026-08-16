# ADR-191 — Quitting mid-edit commits before it closes

**Date:** 2026-08-16
**Status:** Implemented
**Related:** ADR-109 and its follow-up (`tests/test_shutdown_refresh_guard.py`) — the same crash from the *refresh* side, and the `repo.is_open()` guard this reuses. ADR-179 (transient DB errors must degrade, not crash). ADR-057/059 (the flush-and-close-on-exit contract this must not weaken). ADR-184 (which made the budget drill-down an editable register, widening the surface).

## Context

Owner-reported on quit:

```
ProgrammingError: Error calling Python override of QStyledItemDelegate::setModelData():
Error calling Python override of QAbstractTableModel::setData():
Cannot operate on a closed database.
```

The sequence: `RegisterWindow._flush_and_close` closes the Repository on quit (via `closeEvent` or the Cmd-Q `on_about_to_quit` path, ADR-109). Qt then tears the window down — and tearing down a view **with a cell editor still open commits that editor on the way out**. The commit runs `delegate.setModelData` → `model.setData` → `Repository.is_reconciled`, against a connection that closed moments earlier.

The doubled *"Error calling Python override"* wrapper is the signature of the exception crossing two Qt virtuals on its way out, which is also why it arrives with no useful traceback.

**This is the ADR-109 follow-up's bug wearing different clothes.** That one was a queued `WindowActivate` reaching a secondary window's *refresh* after the close; the fix was to guard the refresh on `repo.is_open()`. Nobody checked whether anything else could reach the Repository after close. An open editor can, on a path nothing queues — Qt does it synchronously during destruction.

**Why it went unnoticed:** it needs a cell editor to be *open* at the moment of quit. Committing an edit with Enter or a click elsewhere closes the editor first, so the ordinary rhythm of editing never hits it. Only quitting with the cursor still in a cell does.

## Decision

**Commit open editors before closing the connection, and refuse writes after it.** Two changes, in that order of importance:

- **Ordering (`RegisterWindow._commit_open_editors`, called first thing in `_flush_and_close`).** Clear focus from the focused widget while the Repository is still open. An item-view editor commits on focus-out through the delegate's own event filter, so the edit lands **through the ordinary path, at a moment when it can succeed**. This is the fix that preserves the user's work: quitting mid-edit now *saves* what was typed instead of losing it. Wrapped in a bare `except` — nothing here may block a quit.
- **Guards (`TransactionTableModel.setData`, `BudgetWindow._on_edit_allocation`).** Return `False` when `repo.is_open()` is false. The backstop for every teardown order the ordering fix doesn't cover — a queued event, a secondary window, a delegate that commits later than expected. Declining a write is the correct answer at that point: there is nowhere to put it.

The budget guard sits **before** the copy-forward scope prompt, not after. Reaching the prompt would put a modal question on screen during shutdown, which is worse than losing a half-typed figure.

## Rejected

- **Guarding `setData` alone.** Simplest, and it stops the crash — but it silently discards an edit the user had every reason to think was saved. The crash was the loud symptom; quiet data loss on quit would be the durable one.
- **Committing editors alone, without the guards.** Fixes the reported path and leaves the class of bug open. The ADR-109 follow-up already showed that "the connection is closed and something still reached it" recurs by routes nobody enumerated in advance.
- **Closing the Repository later — after the widgets are destroyed.** Correct in principle and a much larger change: the close is what checkpoints the WAL into the `.mfl` (ADR-057's auto-save-on-exit), and deferring it past widget teardown means deferring it past the point where a crash loses the checkpoint entirely. Worse failure mode for a rarer bug.
- **`QApplication.processEvents()` before the close** to drain pending commits. Doesn't help — the commit isn't queued, it happens synchronously inside widget destruction, after this point either way.
- **Catching `sqlite3.Error` inside `setData` and swallowing it.** Turns the crash into a silent failure at the same place, without the `is_open()` check's ability to distinguish "shutting down" from "a real database error worth surfacing".

## Consequences

- Quitting with a cell editor open no longer crashes, and **keeps the edit**.
- Any write that still arrives after the close is declined rather than fatal — including from surfaces not touched here, since the register model is shared by the all-transactions view, the security drill-downs and (ADR-184) the budget drill-down.
- `is_open()` runs a `SELECT 1` per `setData` call. It is one statement on an already-open connection, against an edit that is about to do real work; not measurable.
- The two editable surfaces are guarded; **a third editable model added later would need its own**. The failure mode is loud (this exact crash), which is some protection, but not a substitute for remembering.

4 new tests in `tests/test_quit_with_open_editor.py`: the exact `setData`-after-close call Qt makes; a full quit with a live editor open; the in-flight value surviving into the reopened file; and the budget matrix's equivalent. **All four fail against the unfixed code**, and the failing run reproduces the owner's message verbatim — `sqlite3.ProgrammingError: Error calling Python override of QAbstractTableModel::setData(): Cannot operate on a closed database` at `register_model.py:288`, `self._repo.is_reconciled(row.id)`.

**Both modals on the budget path had to be stubbed for the test to be useful.** Without the guard, `_on_edit_allocation` reaches the copy-forward prompt *and then* catches the resulting `ProgrammingError` into a `QMessageBox.critical` — two dialogs that each wait forever, so the first version of this test hung for ten minutes instead of failing. Stubbing them to *recorded* answers keeps the assertion honest: reaching either one at all is now the failure.
