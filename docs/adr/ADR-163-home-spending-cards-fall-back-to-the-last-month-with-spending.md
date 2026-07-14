# ADR-163 — Home's spending cards fall back to the last month with spending

**Date:** 2026-07-13
**Status:** Implemented
**Related:** ADR-161/162 (the same design review). ADR-075 (the Home dashboard and its self-hiding cards). ADR-156/160 (Home is on the hot path — don't add work to it).

## Context

From the design review. Home's **Top Payees · This Month** and **Top Categories · This Month** both read:

> *No spending yet this month.*

…with nothing under them, and the Budget card above them showed `£0.00 of £2,474.00` over an empty progress bar. **Three dead cards down the right-hand column**, which reads as a broken app rather than as a quiet month.

The cause is that the two spending cards window on **month-to-date** (`today.replace(day=1)` → `today`). That window is legitimately empty in two ordinary situations:

- **On the 1st–2nd of any month.** Every user sees this, every month.
- **On any file whose data stops earlier** — which is every file between imports, and is the state the demo file is permanently in.

So the emptiest the dashboard can possibly look is a state a real user hits routinely, through no fault of their data.

## Decision

**If the current month has no spending, fall back to the last month that does — and name it.**

The cards title themselves from the period they actually cover: `TOP PAYEES · JUNE 2026` instead of `TOP PAYEES · THIS MONTH`. The label is not decoration — it is what makes this honest. A figure labelled "this month" that is silently really June's would be a *worse* bug than the empty card: the whole reason ADR-159 was dangerous is that a wrong number is more believable than an obviously-missing one.

`Repository.latest_spending_month(not_after)` is **one indexed `MAX(substr(posted_date,1,7))`**, not a loop of per-month probes. Home is on the hot path (ADR-156/160 exist precisely because it wasn't), and an active file — one with spending this month — answers this from the first row it touches and then uses the normal window unchanged. A file with no spending at all keeps the empty "This month" card, because that *is* the honest answer.

The query mirrors the report aggregates' definition of spending — an expense-kind category, excluding transfers and portfolio moves — so the month it names is a month the cards will actually find rows in. Getting that definition subtly different would produce the worst possible outcome: a card titled "June 2026" that is still empty.

## Rejected

- **Falling back silently, keeping the "This month" title.** A lie. See above.
- **Widening the window to "last 30 days" always.** It would fix the empty card, but it changes what the card *means* for every user on every day, to solve a problem that only exists when the month is empty. The month is the unit people budget in.
- **Hiding the cards when empty** (the ADR-075 self-hiding behaviour). Hiding two of the four right-hand cards makes the dashboard lopsided, and it removes the affordance — those cards are the entry points to the Payee and Spending reports.
- **Changing the Budget card too.** Deliberately left alone. "You have spent £0 of your £2,474 budget this month" is a *true and useful* statement about a month that has just started — unlike an empty top-payees list, which tells you nothing. The budget's period is the month by definition, and pointing it at June would misstate what is budgeted now.

## Consequences

- The dashboard is populated on the 1st of the month, and on any file between imports. Verified by re-rendering Home against the demo file (whose data stops in June): both cards now show real rows, titled `· JUNE 2026`.
- The two cards can now describe a **different period from the Budget card** sitting next to them. That is the intended, labelled trade — and it is why the label is mandatory.
- One extra query per Home refresh, on an indexed `MAX`. Negligible against the ~400 ms the refresh already costs, and it does not fire per-card.
- The empty-state text is now "No spending recorded yet." — reached only when the file has *no* spending anywhere, where it is accurate.

`tests/test_home_spend_period_fallback.py` 7/7 (the lookup and its upper bound; the current-month case unchanged; the fallback labelled and covering the *whole* fallback month; the month-end across a year boundary; and — the point — that the cards actually have rows afterwards). Full suite 316/316. No schema change.
