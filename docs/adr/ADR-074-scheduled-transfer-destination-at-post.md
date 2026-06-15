# ADR-074 — Capture a scheduled transfer's destination at post time

**Date:** 2026-06-15
**Status:** Accepted
**Related:** ADR-023 (scheduled transactions), ADR-020 (category-driven transfers), ADR-027 (Create Schedule From Transaction), ADR-035 amendment (cross-currency partner amount at post). Owner-reported bug.

---

## Context

Posting a transfer-kind scheduled transaction that had **no destination account** failed with `"Transfer schedule is missing a destination account."` (raised by `post_scheduled_txn`) and gave the user no way to fix it — the error just blocked the post.

A transfer schedule normally gets its destination at setup: `ScheduleDialog` reveals an inline destination picker when the selected category is transfer-kind and validates it on save. But a schedule can still end up transfer-kind *without* a destination:

- its category was created as expense/income and **later switched to a transfer kind** (the stored `transfer_to_account_id` stays NULL while `category_kind` becomes `transfer`), or
- it was seeded from a transaction (ADR-027) in a path that didn't carry one.

The launch auto-post sweep (`auto_post_due`) already swallows such failures per-schedule, so the only visible breakage was the manual **Post Now** flow. The owner's suggestion: capture the destination either at setup or at posting.

---

## Decision

**Capture the missing destination at post time, and persist it to the schedule.**

In `SchedulesDialog._on_post_now`, before the amount prompt / confirm, when the schedule is transfer-kind and `transfer_to_account_id is None`:

1. Open the existing `TransferDestinationDialog` (unlocked — the user picks any account other than the source). If there's no other account, explain that and stop.
2. On accept, persist the choice via the new `Repository.set_scheduled_transfer_destination(schedule_id, account_id)` (validates it differs from the source), then re-read the schedule.
3. Continue the normal flow — the fixed-amount confirm now shows the chosen destination, and the existing cross-currency block collects the partner amount if the two accounts differ in currency.

Persisting (rather than posting once with an ad-hoc destination) means the schedule is repaired: future manual posts *and* the auto-post sweep work, and the Schedules list shows the destination. Setup and edit already cover their own cases (the dialog requires a destination whenever the category is transfer-kind), so this closes the one remaining gap.

---

## Consequences

- The reported error path is gone: Post Now on a destination-less transfer schedule now prompts instead of failing, and the fix sticks.
- One new focused Repository method; the UI reuses the existing `TransferDestinationDialog` (same widget the register's inline-transfer and cross-currency flows use), so the destination picker and cross-currency amount entry behave identically everywhere.
- No schema change, no migration.

### Rejected alternatives

- **Post once with a one-off destination (don't persist)** — would re-prompt every occurrence and leave the auto-post sweep still silently skipping it. Persisting repairs the template.
- **Only fix it at setup/edit** — doesn't help a user who hits the error at post time and doesn't know the schedule is mis-configured; the post-time prompt is where the problem actually surfaces.
- **Block creation of any transfer schedule without a destination harder** — already enforced in `ScheduleDialog`; the real cause is a category kind changing *after* creation, which no setup-time guard can prevent.

---

## Verification

Offscreen: a transfer-kind schedule inserted with `transfer_to_account_id = NULL` raises on `post_scheduled_txn`; after `set_scheduled_transfer_destination` it posts both halves with a shared `transfer_id` and correct signs; setting the destination equal to the source is rejected. Offscreen Qt: driving `SchedulesDialog._on_post_now` with the destination dialog stubbed to return an account persists the destination, posts both halves, and emits `schedules_changed`.
