# ADR-190 — Bulk edit keeps an existing transfer pairing

**Date:** 2026-08-16
**Status:** Implemented
**Related:** ADR-020 (category-driven transfers — the rule that picking a transfer-kind category asks for the other side). ADR-036 (the bulk match-or-create flow this narrows). ADR-035 (the cross-currency destination dialog). ADR-131/ADR-041 (the register's date window, which made the first version of these tests vacuous).

## Context

Owner-reported from a screenshot: a dozen Ikea Credit Card rows, every one already paired to Chase Checking by Transfer Reconcile — memo *"Transfer from Chase Checking"*, status Matched. Bulk-editing them onto a **different** transfer category popped the destination picker: *"You picked a transfer category. Which account is the other side for these transactions?"*, pre-filled with **Ally Savings**.

The owner's objection is exact: the other side was chosen already. Asking again invites an answer that contradicts the pairing on disk.

**The inline path already knew this.** A single-row category edit returns early when the row has a `transfer_id` (`register_window.py`, the `_on_data_changed` guard) — an already-paired row takes the new category and nothing else happens. The bulk path never learned the same lesson: it branched on the *category kind* alone and ran the whole destination → matcher → review → pair flow over every selected row, paired or not.

**What actually happened is not what it looks like, and the difference matters.** The owner's stated fear was duplicate transactions on the target account. The repository does not allow that: `_convert_to_transfer_unbatched` raises *"Transaction N is already part of a transfer"*, `_link_transfer_unbatched` raises *"Source is already part of a transfer"*, and `bulk_match_or_create_transfers` rolls the whole batch back on either. No duplicate partner could ever be written.

So the damage was smaller than feared but still real, and in three parts: a question the user had already answered, pointed at the wrong account; then — if they answered it — a hard failure that **also discarded the pairing work for any rows in the same selection that genuinely needed it**, because the batch is atomic; and all of that *after* the phase-1 field update had already committed separately, so the category change stuck while the pairing failed. The one thing never at risk was the data.

## Decision

**Partition the selection on `transfer_id` before deciding whether to ask anything.**

- **Already-paired rows are recategorised, never re-paired.** Moving a transfer from one transfer category to another is a recategorisation; there is no second side to ask about, which is precisely what the inline single-row edit has always done.
- **Every selected row is already paired → no prompt at all.** Straight to `bulk_update_transactions` with the category and any ticked payee/status/memo. This is the reported case. No amounts move, so the sidebar totals are left alone, matching the plain bulk-edit path.
- **A mixed selection still prompts — for the unpaired rows only.** They are the only ones that reach the matcher, the review dialog and the pairing batch. The prompt names their count and adds a line saying how many rows are already transfers and will keep the pairing they have.
- **Only unpaired rows constrain the destination picker.** The exclusion set exists to stop a transfer to its own account; an already-paired row's account is a perfectly good destination for the others, and excluding it was blocking legitimate answers.
- The status message reports what happened to both groups.

## Rejected

- **Blocking the bulk edit when the selection contains a transfer**, with "remove them from the selection first" (the pattern ADR-051 uses for splits). Splits genuinely *cannot* become transfers; these rows already are, and recategorising them is both meaningful and what the owner asked for. Refusing would make the user do by hand what the app can do correctly.
- **Re-pointing the pairing at the newly-chosen account** — treating the prompt as an instruction to move the other side. It is a plausible reading of a destination picker, and it would rewrite a partner the user established elsewhere on the strength of a pre-filled combo they never deliberately set. Moving a transfer's other side is Transfer Reconcile's job.
- **Leaving it to the repository's guard.** It is the reason this was never corruption and it stays, but a loud failure after a needless question is not a fix — especially when the atomic batch means the paired rows take the legitimate ones down with them.
- **Propagating the new category to the partner row.** Out of scope and a separate question: neither `update_transaction_category` nor `bulk_update_transactions` does this today, so a transfer's two halves can already carry different categories. Changing that is a decision about transfer semantics, not about this prompt.

## Consequences

- The reported flow now silently does the right thing: categories change, pairings stand, no dialog.
- A mixed selection is no longer all-or-nothing — the unpaired rows get paired even though the paired ones are in the selection, where previously the batch failed as a unit.
- The two paths through bulk edit (paired / unpaired) now match the two paths through the inline edit, so the same rule holds however the user gets there.
- **`transfer_id` must be populated on the register's model rows** for the partition to work. It is (`list_transactions_for_account` selects it), and a test asserts the paired rows keep their original partner account, which fails if it ever stops being.
- No schema change, no repository change.

7 new tests in `tests/test_bulk_edit_keeps_existing_transfer.py`, split between the repository's refusal to re-pair (the reason this was never duplication — including a direct assertion that the target account's transaction count does not move on the failing path) and the register's partition. Two fail against the unfixed code with *"the destination picker was opened for paired rows"*.

**Two things the tests had to get right to be worth anything.** The register opens on a **12-month window** (ADR-041), so a fixture dated 2024 produced an empty model, an empty selection, and assertions that passed while testing nothing — the helper now sets the window to `all` through `set_since`, the same call the Show combo makes. And the review dialog is stubbed **even in the tests that should never reach it**: against the unfixed code they do reach it, and an unstubbed modal hung the run for ten minutes instead of failing. A test whose failure mode is a hang cannot tell you what broke.
