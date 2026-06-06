# ADR-027 — Create Schedule From Transaction (right-click verb)

**Date:** 2026-06-06
**Status:** Accepted
**Related:** ADR-020 (Account transfers — partner-row handling carries into the seed); ADR-022 (Register typeahead delegates + inline category create — the same right-click context menu the new verb sits on); ADR-023 (Scheduled transactions — the `ScheduleDialog`, `create_scheduled_txn`, and `compute_next_due_date` primitives this verb consumes); CLAUDE_CONTEXT polish backlog item "make a schedule by hand by retyping every field" (closed by this ADR).

---

## Context

Owner's request: a right-click "Create Schedule From Transaction" verb on the register. The most common reason to create a schedule is *this txn repeats* — recurring rent, paycheck, gym subscription, Sunday lunch standing order. Typing the payee/amount/category/account into the schedule dialog from scratch when the user is staring at a representative row is wasted keystrokes; the existing row already has every field the schedule needs except cadence + end-date.

Existing primitives this leverages (all from ADR-023):

- `ScheduleDialog` — the modal that gathers schedule fields, validates them, and emits a `ScheduleDialogValues` dataclass. Already supports the dialog-internal logic for transfer-kind categories (reveals destination-account combo when the picked category's kind is `transfer`).
- `Repository.create_scheduled_txn` — atomic insert + validation.
- `Repository.compute_next_due_date(anchor, cadence, current_due)` — the anchor-aware "what's the next occurrence" math from ADR-023. Handles Jan 31 → Feb 28 → Mar 31 correctly.
- `Repository.expand_transfer_partners(ids)` and the `txn.transfer_id` column from ADR-020 — already encode whether a txn is half of a transfer.

The verb sits on the register's existing `customContextMenuRequested` infrastructure (added when ADR-017 brought right-click verbs to the table).

## Options considered

### Where the seed values are passed into the dialog — constructor kwargs / `existing=` reuse / new `seed=` parameter (chosen: new `seed=` parameter)

- *Constructor kwargs*: add a dozen positional defaults to `ScheduleDialog.__init__` (default_payee, default_amount, default_category_id, etc.). Lots of new optional kwargs, easy to lose track of which are seed vs structural.
- *Reuse the existing `existing=ScheduledTxnRow` parameter*: build a fake `ScheduledTxnRow` from the txn and pass it as `existing`. Triggers edit-mode wiring (title "Edit Schedule", next-due-date row visible, save path calls `update_scheduled_txn`). Wrong — the user is *creating* a schedule, not editing one that exists.
- **New `seed: Optional[ScheduleSeed]` parameter on `ScheduleDialog`** (chosen): a frozen dataclass carrying the pre-fill values. Dialog stays in create mode; seed values are applied where the existing `if is_edit ... else <default>` ternaries already lived, becoming `if is_edit ... else (seed.value if seed else <default>)`. Edit-mode logic untouched. Extensible — future "create schedule from <something else>" entry points (e.g. a payee-management dialog) reuse the same seed shape.

### How the source txn's date maps onto the schedule's date fields (chosen: pre-compute the next future occurrence; anchor = next_due = that future date)

- *txn date → anchor, next_due = anchor* (the dialog's normal create default): the source txn becomes the *first* occurrence. But the txn already happened. Would trigger an immediate "overdue / post now" on the next auto-post sweep — surprising.
- *txn date → anchor; handler overrides next_due = anchor + cadence* (the initial ADR-027 decision, **superseded 2026-06-06 same day by owner feedback**): preserved the rhythm correctly (Jan 31 → Feb 28 → Mar 31), but the dialog confusingly showed "First occurrence (anchor): 2026-05-25" with the source txn's *past* date, then silently saved the schedule with a different next_due. Owner reaction: "the dialog says 'first occurrence' and shows the occurrence of the current transaction. I think we should say 'Next occurrence' which should be the first date of the transaction in the future." Behavioural split between this flow and the from-scratch flow was clever but invisible — the user couldn't see what next_due was about to be, only the misleading anchor field.
- **Pre-compute the next future occurrence in the right-click handler, pass it as `seed.anchor_date`, and surface it in the dialog as "Next occurrence:"** (chosen): the handler steps forward from `txn.posted_date` by the default cadence until the result is strictly after today (one step for last-month txns; iterates for older ones). That date is what the user sees in the dialog. On save, anchor and next_due both store that future date — the same shape every other "New Schedule" produces. No behavioural split: one dialog flow, one storage shape, one mental model.

The dialog field label changes from "First occurrence (anchor):" to "Next occurrence:" in *both* flows (seeded and from-scratch). The "(anchor)" technical word is dropped — it was leaking implementation into the UX. Internally the field still binds to `anchor_date`; what's stored doesn't change.

**Trade-off accepted:** rhythm drift on the 29/30/31-of-month edge cases. A Jan 31 source txn pre-computes a Feb 28 next-occurrence; the schedule's anchor becomes Feb 28 and subsequent occurrences stick to the 28th forever instead of bouncing back (Mar 31, Apr 30…). The previous design preserved this rhythm by keeping anchor=txn-date; the simpler design loses it. Owner accepted this — 29/30/31-of-month bills are rare, and the schedule's anchor is editable after-the-fact (edit mode still shows the anchor field explicitly). If the rhythm matters for a specific schedule, the user can hand-edit the anchor.

### Default cadence (chosen: monthly)

- *Heuristic from the txn's history* — look up other txns from the same payee/category at the same amount, infer a cadence from the spacing. Smart, but easy to get wrong on sparse data; sets a UX expectation that won't survive the first weekly payday or quarterly insurance bill.
- **Monthly** (chosen): user-asked-for default; covers the most common case (subscriptions, rent, bills); the dialog's cadence dropdown is one click away when the inference is wrong. If owner builds intuition over real use that monthly is wrong more often than right, revisit with a heuristic then.

### Transfer-half rows — pre-fill destination / leave blank / disable verb (chosen: pre-fill destination from partner)

- *Leave blank* and let the user pick: works but defeats the point of seeding. Transfer-kind categories require a destination; making the user re-pick the same destination they're already looking at on the partner row is the same wasted keystrokes the whole verb is trying to eliminate.
- *Disable the verb for transfer-half rows*: avoids the partner lookup but blocks the perfectly reasonable "this transfer repeats every payday" use case.
- **Pre-fill destination from partner** (chosen): one extra Repository call (`get_transfer_partner_account_id`), seed populates `transfer_to_account_id`, dialog reveals the destination row pre-set. New method added to Repository — small and self-contained (single-row SQL query against the same `transfer_id`). Right-click verb works identically on a transfer-half row as on a regular row.

### Multi-row right-click — single-row only / per-row schedules / merged seed (chosen: single-row only)

- *Per-row schedules in one go*: 20 rows → 20 schedules. Bulk. Not what the verb is for; can easily be done one at a time from a filtered view if it's ever needed.
- *Merged seed* (intersect common fields, leave the rest blank): clever but ambiguous — what's the amount when the rows differ? Cadence inferred from row spacing? Too magical.
- **Disable the verb when more than one row is selected** (chosen): the menu item only appears when exactly one txn is in the selection. Simple, predictable, no edge cases.

### Where the verb appears in the context menu (chosen: just under "New Transaction", with separators)

- *Bottom of the menu*: groups with destructive verbs (Delete). Wrong neighbourhood — Create Schedule is a constructive verb.
- *Top-level Transaction menu*: discoverable but adds another menu entry. The right-click is the more natural entry from the user's task ("I'm looking at this rent payment and want to set it up to repeat").
- **Right under "New Transaction", with a separator before Bulk Edit / Delete** (chosen): keeps constructive verbs together at the top of the menu. The existing context menu had no separators; this ADR adds three (one above schedule, one above bulk-edit, one above delete) so the verb groups visually instead of running together.

## Decision

- Add a right-click menu item **"Create Schedule From Transaction…"** on the register table. Visible only when exactly one txn row is selected. Sits just under "New Transaction" with separators around it.
- Introduce `ScheduleSeed` (frozen dataclass) in `mfl_desktop/ui/schedule_dialog.py`. Fields: `account_id`, `payee_name`, `category_id`, `transfer_to_account_id`, `amount` (signed), `anchor_date` (ISO), `cadence`, `memo`. All `Optional`.
- Add a `seed: Optional[ScheduleSeed] = None` parameter to `ScheduleDialog.__init__`. Pre-fills the form in create mode; edit mode ignores the seed if both are passed. Dialog title stays "New Schedule".
- Handler in `RegisterWindow._on_create_schedule_from_txn` steps the txn date forward by the default cadence until it lies strictly after today, builds the seed with that future date as `anchor_date`, opens the dialog (which surfaces that date as **"Next occurrence:"**), and on accept calls `Repository.create_scheduled_txn` with the dialog's returned `anchor_date` and `next_due_date` (both = the future date the user saw). Status-bar confirmation: `"Schedule created · next due YYYY-MM-DD"`.
- Dialog field label changes from "First occurrence (anchor):" to **"Next occurrence:"** in both seeded and from-scratch flows. Internally the field still binds to `anchor_date`; no storage change.
- Add `Repository.get_transfer_partner_account_id(txn_id) -> Optional[int]` — one SQL round-trip that returns the partner row's `account_id`, or `None` for non-transfer rows. Used by the handler to pre-fill `transfer_to_account_id` on transfer-half rows.
- Default cadence is `"monthly"`. Default anchor is the source txn's `posted_date`. Default `auto_post` is `False`. Default `end_date` is unset.

## Consequences

**No schema change.** Everything reuses ADR-023's primitives. `scheduled_txn`, `create_scheduled_txn`, `compute_next_due_date`, `ScheduleDialog`, the register context menu — all already there.

**One create flow for schedules.** Both Manage → Schedules → New… and the right-click verb produce identically-shaped schedules: anchor = next_due = the date the user picked (or that the right-click flow pre-computed). The earlier ADR-027 design split these into two flows; owner feedback collapsed the split.

**Rhythm drift on 29/30/31-of-month seeds.** A schedule seeded from a Jan 31 txn will fire on Feb 28 (next-future), then Mar 28, Apr 28… instead of bouncing back to Mar 31 / May 31 etc. Acceptable: edge case, recoverable by hand-editing the schedule's anchor in edit mode (which still shows the field).

**Edit shows the anchor explicitly.** Edit mode (unchanged) surfaces both the anchor and next-due rows so the user can correct rhythm drift or skip a missed occurrence.

**Transfer pre-fill is one query.** `get_transfer_partner_account_id` runs only when `txn.transfer_id IS NOT NULL` (cheap guard before the call). For non-transfer rows the verb skips the query entirely.

**Seed dataclass is extensible.** A future "Create Schedule From Payee" verb (from the payee-management dialog) reuses the same `ScheduleSeed` shape. Same for any other entry point that wants to pre-fill the schedule dialog with non-edit-mode values.

**Reversible.** The whole verb is: one dataclass, one Repository method, one menu item, one handler, ~30 lines of seed-aware ternaries in the dialog. Removing it is a single revert per file.

**Not solved.** A back-link from a materialised txn to the schedule it came from. ADR-023 deliberately deferred this; this ADR doesn't change that — the txn knows nothing about which schedule (if any) is its template. A "Convert Txn To Existing Schedule" verb (link rather than create) is also out of scope.
