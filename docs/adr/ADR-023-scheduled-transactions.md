# ADR-023 — Scheduled transactions (bills, recurring income, recurring transfers)

**Date:** 2026-06-05
**Status:** Accepted
**Related:** ADR-010 (Transactional schema design — `txn` row produced by posting a schedule); ADR-012 (Payee model — `get_or_create_payee` reused on post); ADR-014 (Category kind — schedule's category kind dictates whether posting goes through `insert_transaction` or `create_transfer`); ADR-016 (File save / open model — schedule-state advancement is committed straight to disk like any other write); ADR-020 (Account transfers — transfer-kind schedules materialise via the existing `create_transfer` plumbing); ADR-022 (Register typeahead — `make_category_picker` reused for the schedule dialog's category field). First half of the budget arc; ADR-024 (budget core) and ADR-025 (budget visualisations) build on top.

---

## Context

The owner's budget design (captured in the planning conversation on 2026-06-05) sets a hard requirement:

> "The budget screen should show an overall position for the month, which would be the carried-forward balance for that month, minus any actual spending, minus planned spending or transfers."

"Planned" includes both *category-budgeted* amounts the user hasn't spent yet AND *future-but-known* outflows like a £15.99 Netflix subscription due on the 7th, a £1,200 mortgage on the 1st, or a £500 standing transfer to savings on payday. The category budget covers the first kind; the second needs a schedule primitive: "a transaction we know is coming on date X, repeating every <cadence>."

The existing schema has no scheduled-transaction concept. The only "recurring" surface is the reserved `rule` table — but rules are categorisation triggers ("transactions whose memo contains 'TFL' get category Transport"), not transaction templates. Conflating the two would couple two independent verbs (auto-categorise an inbound transaction vs. spawn a new outbound transaction) and make both harder to evolve. So this is a fresh primitive.

Because the budget round (ADR-024) depends on knowing the timing of future outflows, schedules ship first. They're also useful on their own: even before the budget screen exists, having "Netflix £15.99 on the 7th" auto-create as a Pending transaction every month reduces manual entry and improves the register's faithfulness.

This ADR is scoped narrowly to **scheduled transactions as a primitive plus a Schedules management dialog**. Budget-screen consumption of the primitive is ADR-024's responsibility.

## Options considered

### Primitive shape — schedule-as-template / generate-N-occurrences-upfront / cron-job-table (chosen: schedule-as-template)

- *Generate N occurrences upfront*: when the user creates a schedule, materialise the next 12 (or N) occurrences as Pending `txn` rows immediately. Pros — every future row is queryable as a regular txn. Cons — the register fills up with grey "not yet happened" rows that pollute filter/search/balance running totals; editing the schedule (cadence change, amount edit) requires re-finding and re-writing every still-future generated row; the user's mental model of "this is the bill template; the txns are what actually happened" collapses.
- *Cron-job table*: schedule rows reference a generic "next fire" hook that an external job runs. Pros — extensible. Cons — desktop app, no external job runner. Rejected.
- **Schedule-as-template, post on demand or via launch sweep** (chosen): a `scheduled_txn` row stores cadence + estimated amount + next due date. Materialising one occurrence ("posting") creates exactly one `txn` and advances `next_due_date` by one cadence step. Past materialised txns are independent records — editing the schedule does not retro-edit them. The schedule is the template; the txn is the truth.
  - Matches Banktivity's and YNAB's mental model.
  - Lets the budget projection look at `(scheduled_txn rows whose next_due_date is within period AND archived_at IS NULL)` for planned outflows without polluting the register.

### Posting trigger — manual / auto-on-due / manual-with-per-schedule-auto-opt-in (chosen)

- *Manual only*: every post is a user action. Safest, but a fixed Netflix subscription that the user just forgot to post becomes invisible to the budget projection.
- *Auto on due*: every schedule auto-posts on its next-due day when the app launches. Lowest friction for fixed bills, surprising for variable bills (the materialised amount is whatever the schedule's estimate was, not the real number the user is about to see on a statement).
- **Manual by default, per-schedule auto-post flag** (chosen): new schedules default to manual posting; a per-schedule `auto_post` boolean lets the user mark genuine fixed bills (Netflix, mortgage, council tax) for auto-materialisation on launch. The opt-in keeps the safer default while allowing the genuinely automatic case to be automatic.
  - Launch sweep is implemented as `Repository.auto_post_due(today)`. It iterates schedules with `auto_post=1` whose `next_due_date <= today`, posts each, and re-evaluates because each post advances `next_due_date` — so a schedule that's three months overdue catches up in one launch.
  - The Schedules dialog's **Post Now** button drives the manual path for the rest.

### Variable amounts — fixed only / estimated + variable flag (chosen)

- *Fixed only*: schedule stores one amount; if the real bill differs, edit the materialised txn after posting. Simple but loses the budget benefit (a "council tax £180" schedule when the real bill is £198 produces a budget that's quietly £18 short every month until the user notices).
- **Estimated amount + variable flag** (chosen): schedules carry an `estimated_amount` plus a `variable` boolean. For fixed schedules, post uses the estimate as-is. For variable schedules, the dialog prompts for the actual amount at post time (`QInputDialog.getDouble`) and re-signs it according to the schedule's direction. The budget projection uses the estimate as the planned-spending number; the actual replaces it on post.
  - Sign convention: estimated is signed (matches `txn.amount`). Direction is fixed by the schedule — a positive-estimate income schedule won't accept a negative actual amount on post and vice versa. Catches a class of bug where the user types the actual as a refund-sign by accident.

### Cadence shape — full RRULE / fixed enum / fixed enum + anchor-based math (chosen)

- *Full RRULE / iCalendar*: standard, infinitely flexible. Overkill — the owner asked for five values, no exceptions.
- *Fixed enum, advance from current_due*: just `current_due + interval`. Breaks on monthly-on-31st (Jan 31 → Feb 28 → Mar 28 instead of Mar 31), which is exactly the council-tax case.
- **Fixed enum (`weekly`, `biweekly`, `monthly`, `quarterly`, `annual`), anchor-based advancement** (chosen): each schedule stores `anchor_date` (the first occurrence) and `next_due_date` (where we are now). The advance helper does `current_due + 7/14 days` for weekly/biweekly and `min(anchor.day, days_in_target_month)` for monthly/quarterly/annual — so a Jan 31 monthly schedule produces Jan 31 → Feb 28 → Mar 31 → Apr 30, and a Feb 29 annual schedule produces 2024-02-29 → 2025-02-28 → 2026-02-28 → 2027-02-28 → 2028-02-29.
  - One global anchoring rule per the planning conversation — weekly always starts on whatever day-of-week the anchor lands on, quarterly is anchor-month + 3/6/9, annual is anchor-month + 12. Per-category overrides rejected as over-engineered for the owner's actual workflow.

### Transfer-kind schedules — separate type / reuse category-as-verb (chosen)

ADR-020 established that **the category is the verb** for transfers — picking a `kind='transfer'` category on a transaction is what makes it a transfer. The same logic applies to schedules:

- *Separate type column*: `scheduled_txn.kind IN ('income', 'expense', 'transfer')`. Duplicates the information that's already on the category, and the dialog has to pick both consistently.
- **Reuse category-as-verb** (chosen): no `kind` column on `scheduled_txn`. The category's `kind` determines the post behaviour. Transfer-kind schedules carry a `transfer_to_account_id` (nullable; required when the category is transfer-kind) and post via the same `create_transfer` plumbing as inline transfer entry. Non-transfer schedules ignore the field (the repository stamps it to NULL on create/update to keep the column meaningful).
  - Same direction convention as the New Transaction dialog: sign of the estimated amount picks which side is the source.

### Menu surface — Bills / Schedules / split Bills + Income (chosen: Schedules)

- *Manage → Bills & Income*: more familiar wording, but the primitive covers income (salary), expenses (subscriptions, mortgage), AND transfers (savings sweep). Two of those three aren't bills.
- *Two menu entries — Bills and Scheduled Income*: discoverability win but doubles the maintenance.
- **Manage → Schedules…** (chosen): broader term matches the primitive; one dialog, one mental model. The dialog's content makes the distinction clear without splitting the menu.

### Posting-side data flow — direct insert / route through existing methods (chosen)

The post path could either build its own INSERT SQL or call the existing `insert_transaction` / `create_transfer` methods. Routing through the existing methods means a single source of truth for IRI generation, payee resolution, and sign handling — and the same code path that backs the New Transaction dialog backs the Post Now button, so there's no second place for behaviour to drift. For transfers the path is partially inlined (we need to include the schedule's `next_due_date` advancement in the same SQL transaction as the two transfer halves) but uses the same `_insert_transfer_half` helper so the on-disk shape is identical.

### Schedule → materialised-txn link — yes / **deferred** (chosen for this ADR)

A `txn.scheduled_txn_id` back-link would let the register show "this came from schedule X" and let the user trace a real charge to its template. Useful but not needed for v1:

- Editing the materialised txn (e.g. £498 actual vs £500 estimate) doesn't break the schedule — the schedule already advanced.
- The budget projection in ADR-024 doesn't need the link — it queries schedules and txns independently.

Deferred to a future ADR. When added, the migration is additive (new nullable column), and the launch sweep / post path become the only code that has to set it.

### End-of-life handling — never end / end_date with archive (chosen)

- *Never end*: schedules run forever. Fine for subscriptions; wrong for a 24-month phone contract.
- **Optional `end_date`** (chosen): when the post path advances `next_due_date` past `end_date`, the schedule is archived (`archived_at` set) in the same transaction as the materialised txn. Past materialised txns survive — the archive only stops future occurrences.
  - The dialog's end-date editor defaults to disabled with a "(no end date)" checkbox; matches the Qt idiom for optional dates.

## Decision

### Schema — migration 0005

New table `scheduled_txn`:

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `iri` | TEXT UNIQUE | `mfl:Scheduled_<uuid8>` per ADR-006 |
| `account_id` | INTEGER NOT NULL | FK to `account`, ON DELETE CASCADE — deleting an account kills its schedules |
| `payee_id` | INTEGER NULL | FK to `payee`, ON DELETE SET NULL |
| `category_id` | INTEGER NOT NULL | FK to `category` — kind is read from there |
| `transfer_to_account_id` | INTEGER NULL | FK to `account`, ON DELETE SET NULL — required at the app layer when the category is transfer-kind, NULL otherwise |
| `estimated_amount` | INTEGER NOT NULL | Signed pence; matches `txn.amount` convention |
| `variable` | INTEGER NOT NULL | 0/1 |
| `memo` | TEXT NULL | Copied onto each materialised txn |
| `cadence` | TEXT NOT NULL | CHECK in (`weekly`, `biweekly`, `monthly`, `quarterly`, `annual`) |
| `anchor_date` | TEXT NOT NULL | ISO `YYYY-MM-DD`; first occurrence |
| `next_due_date` | TEXT NOT NULL | ISO; advances on post |
| `end_date` | TEXT NULL | ISO; schedule archives when next_due passes this |
| `auto_post` | INTEGER NOT NULL | 0/1 |
| `notes` | TEXT NULL | Private notes on the schedule (not copied to materialised txn) |
| `archived_at` | TEXT NULL | Set when end_date hit; reserved for soft-archive UX |
| `created_at` | TEXT NOT NULL | `datetime('now')` default |

Indexes:

- `idx_scheduled_txn_due_auto` — partial index on `(next_due_date)` filtered to active auto-posting schedules; covers the launch sweep.
- `idx_scheduled_txn_due` — partial index on `(next_due_date)` filtered to active schedules; covers the budget projection (ADR-024).

No back-link from `txn` to `scheduled_txn` in v1 (deferred — see above).

### Repository — new methods + dataclass

- **`ScheduledTxnRow`** dataclass: schedule joined with account/payee/category/transfer-to-account names + category kind.
- **`SCHEDULE_CADENCES`** module-level tuple.
- **`new_scheduled_txn_iri()`** IRI helper.
- **`list_scheduled_txns(include_archived=False)`**, **`get_scheduled_txn(id)`**, **`list_schedules_due_through(date)`** — read paths.
- **`create_scheduled_txn(...)`**, **`update_scheduled_txn(id, ...)`**, **`delete_scheduled_txn(id)`** — CRUD with validation (cadence enum, transfer-kind → destination required, non-zero amount, destination ≠ source).
- **`compute_next_due_date(anchor, cadence, current)`** — static helper; the month-arithmetic that keeps Jan 31 → Feb 28 → Mar 31.
- **`post_scheduled_txn(id, actual_amount=None)`** — materialise one occurrence. Branches on category kind: non-transfer goes through `insert_transaction`; transfer-kind builds two `txn` rows sharing a fresh `transfer_id` via `_insert_transfer_half`, mirroring ADR-020. Validates sign of `actual_amount` against the schedule's direction. Advances `next_due_date`. Archives if past `end_date`. One SQLite transaction.
- **`auto_post_due(through_date)`** — bulk launch sweep. Iterates `auto_post=1` schedules whose `next_due_date <= through_date`, posts each, and re-evaluates per-schedule until next_due passes the cutoff so a multi-period catch-up materialises every missed occurrence. Per-schedule failures are silently swallowed (a single bad schedule must not refuse to launch the app); manual schedules surface their errors in the dialog instead.

### UI — two new dialogs + one menu entry

- **`ScheduleDialog`** (`mfl_desktop/ui/schedule_dialog.py`) — create/edit a single schedule. Reuses `make_category_picker` so the category combo matches the register and the New Transaction dialog (ADR-022). A `currentIndexChanged` signal on the category combo toggles a Transfer-to-account row's visibility based on the picked category's kind. Direction is a radio (Money out / Money in); estimated amount is entered positive and re-signed by the direction radio. Variable, auto-post, and end-date (with a "(no end date)" enable checkbox) are the additional editable knobs. Next-due-date editor is only shown in edit mode — on create the repo defaults `next_due_date` to the anchor.
- **`SchedulesDialog`** (`mfl_desktop/ui/schedules_dialog.py`) — list view + CRUD + Post Now. `QTableWidget` columns: Account / Payee / Category / Estimated / Cadence / Next due / Auto / Var. Bottom summary strip: "X schedules · Y due in the next 30 days". Buttons: New Schedule…, Edit…, Post Now, Delete. Post Now branches on the `variable` flag (prompt for actual amount) vs fixed (single confirm dialog). Transfer-kind schedules' confirm dialog names both source and destination so the two-row side effect is explicit. Emits `schedules_changed` after every mutation so the register window can refresh.
- **Menu**: `Manage → Schedules…` slotted in after `Manage → Categories…`. Single menu entry; no toolbar in this round.
- **Launch-time auto-post sweep** (`RegisterWindow._run_auto_post_sweep`): called at the end of `__init__` and at the end of `_swap_repository` (so opening a different `.mfl` file re-runs the sweep against the new file's schedules). Counts the posts; status-bar message only when non-zero so a quiet startup stays quiet. Bare-except guard on the whole sweep — never let auto-post failures block app launch.

### Caller wiring

`RegisterWindow` gains:

- `from datetime import date` and `from mfl_desktop.ui.schedules_dialog import SchedulesDialog`.
- `self._manage_schedules_action` and `_on_manage_schedules` to launch the dialog.
- `_on_schedules_changed` connected to the dialog's signal — reloads the model and refreshes sidebar balances (Post Now materialises a real txn).
- `_run_auto_post_sweep` — calls `Repository.auto_post_due(today)`, reloads model + sidebar if anything posted, shows a status-bar count.

No changes to the model, the proxy, the typeahead delegates, the sidebar, or the existing dialogs.

## Consequences

### Positive

- **The register stays clean** — only materialised transactions show up. Schedules live in their own dialog. Banktivity / YNAB mental model preserved.
- **Variable bills are a first-class concept**, not a special case the user has to remember to edit after the fact. The prompt-on-post flow makes the actual amount the truth without breaking the budget projection.
- **Transfer-kind schedules reuse `create_transfer`** so the on-disk shape of a posted transfer schedule is byte-identical to a manually-created transfer. No second place for transfer behaviour to drift.
- **Anchor-based cadence math handles month-end correctly.** Council tax on the 31st, mortgage on the 1st, and Feb 29 annuals all work the way a user would expect them to.
- **Catch-up sweep is loop-based**, so a user who didn't launch the app for two months gets every missed auto-poster materialised in date order without manual intervention.
- **The new primitive is exactly what ADR-024 needs**: planned spending = `SUM(estimated_amount where next_due_date in [today, period_end])` plus per-category budgeted residuals. No further plumbing required.

### Negative / trade-offs

- **No back-link from materialised txn to source schedule.** The user can't visibly tell which register rows came from a schedule. Deferred (see Options); when added, additive migration + sweep/post-path update only.
- **Auto-post amounts are the estimate, not the actual.** A variable utility bill marked `auto_post=1` would silently post the estimate each cycle, which is wrong. The UI gates this — `auto_post` on a `variable=1` schedule still works, but the user is the one who chose both checkboxes; v2 could refuse the combination outright or convert auto-post-on-variable into "post as Pending with the estimate, flag for user review", but for now the explicit user choice is respected.
- **Per-schedule failures in the launch sweep are swallowed.** The Schedules dialog won't tell the user a schedule was skipped on launch; they'd discover it by opening the dialog and seeing an overdue `next_due_date`. Acceptable for v1; a "Schedule errors at last launch" surfacing could be added later if it bites.
- **`anchor_date` is set once at create time.** If the user changes a Jan 31 monthly schedule's anchor to Jan 15, future month-end clamping recomputes from the new anchor — which is the intended behaviour, but the user who changes anchors as a "skip ahead" verb instead of next-due-date might be surprised. The dialog labels are explicit ("First occurrence (anchor)" vs "Next due date" in edit mode).
- **No per-category cadence anchor.** A user whose paycheck is bi-weekly on Friday and whose rent is monthly on the 1st gets that exactly via per-schedule anchor; but a user who wants weekly schedules to all align to "their week" (e.g. Sunday-start instead of the anchor's day-of-week) doesn't have a knob for it. Per the planning conversation, accepted — single global rule, per-category override deferred.

### Ongoing responsibilities

- **The launch sweep is idempotent per session** because each post advances `next_due_date` past today. Any future change to the post path that re-sets `next_due_date` (e.g. a "reschedule" verb) must preserve this so the sweep doesn't loop forever.
- **`compute_next_due_date` is a pure static helper** — keep it that way. Calendar-arithmetic code with hidden state is the bug magnet category in scheduling code.
- **The transfer-kind branch in `post_scheduled_txn` partially inlines `_insert_transfer_half`** so the schedule's `next_due_date` update is in the same SQL transaction as the transfer pair. If `create_transfer` ever grows additional invariants (e.g. transfer batch table, recon flags), the inlined branch needs to mirror them. Worth a comment at the inlining point — already present.
- **ADR-024's budget projection depends on `list_schedules_due_through`** for planned outflows. If the budget round needs additional fields on `ScheduledTxnRow` (e.g. per-occurrence pro-rating for non-monthly cadences shown on the monthly screen), extend the dataclass + query rather than re-deriving from raw SQL in the budget code.
