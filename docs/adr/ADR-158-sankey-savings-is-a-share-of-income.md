# ADR-158 — The Cash Flow Sankey's Savings node is a share of income, not of expenditure

**Date:** 2026-07-12
**Status:** Implemented
**Related:** ADR-146 (Sankey folds in transfers). ADR-018 (no pies). ADR-055 (display-currency conversion).

## Context

Owner report, looking at the Cash Flow report ("Regular Spending (HSBC)", year to date, Show: Percent): the Savings node reads **98%**. "It looks a little high."

It is wrong, and the report **contradicted itself on the same screen**. The summary rail said:

```
INCOME        £48,978
EXPENDITURE   £24,763
AMOUNT SAVED  £24,216
Saving rate: 49.4% of income
```

while the diagram's Savings node said `98%`. Two numbers, one screen, same quantity, different answers. The arithmetic identifies the bug immediately:

```
24,216 / 48,978 = 49.4%     the rail
24,216 / 24,763 = 97.8%     the diagram  ->  "98%"
```

The Savings node was being divided by **expenditure**.

The mechanism is a denominator picked by geometry rather than by meaning. `sankey_chart.py` chose a node's denominator from which column it sat in:

```python
side_total = self._total_income if col < 0 else self._total_expense
```

That is right for a *category*: an expense category should read as a share of spending ("Household is 28% of what I spend"), an income category as a share of income, and each side's categories then sum to 100%.

But Savings is not a category. It is the **balancing remainder**, `income - expense`, appended to the expense side so that both sides fill the spine (`sankey_report_window.py`, "Balance the shorter side so both fill the spine"). Crucially it is **not part of `total_expense`** — `total_expense` is the sum of the actual spending categories. So dividing Savings by expenditure measures it against a total that *excludes it*, which is why the figure runs away toward 100% whenever savings approaches the size of spending. At exactly break-even spending it would read 100%; if the owner saved more than they spent it would read **over** 100%.

The mirror case was already correct by accident: **Deficit** (`expense - income`, raised when overspending) is appended to the **income** side, so `col < 0` already handed it `total_income` — which is the right denominator and is what the rail's "Overspend: X% of income" also uses. Only Savings was broken, and only because of which side it happened to be parked on.

## Decision

**A node's denominator is chosen by what the node *means*, not by which column it landed in.** A new `SankeyChart._denominator_for(node, col)` states the rule in one place, and both the on-diagram label and the hover tooltip go through it (they already shared a single `side_total`, so one fix corrects both):

- **Balance nodes** (`is_balance` — Savings and Deficit) are a share of **income**.
- **Categories** are a share of their own side's total, exactly as before.
- **The spine** stays a share of the larger side.

Savings and Deficit both being "% of income" is the point: it is the saving rate the summary rail already computes and states in words ("Saving rate: 49.4% of income"), so the diagram and the rail can no longer disagree. It is also what Deficit already did, so the two balance nodes are now symmetric rather than accidentally different.

Owner chose this over the alternatives (asked explicitly, since it changes how the other numbers read).

**The accepted cost:** the expense side now mixes denominators. Its categories are percentages of spending; its Savings node is a percentage of income; so the labels down that side no longer sum to 100%. That is correct rather than sloppy — **Savings is not a spending category and never belonged in that sum.** Adding it in was precisely the error.

### Rejected

- **Make every node a share of the spine (total income).** Internally consistent, sums to 100%, and each label would then match its ribbon's visual thickness — genuinely attractive. But it rewrites the numbers the owner already reads and relies on: Household would drop from 28% to 14%, and "X% of my spending" — the more useful reading for a spending category — would be lost entirely. Rejected for that regression, not on principle.
- **Render Savings as a currency amount even in Percent mode.** Sidesteps the question rather than answering it, and the user asked for percentages.
- **Fold Savings into `total_expense`.** Would make the denominator self-consistent, but it would silently redefine "expenditure" to include money that was *not* spent — corrupting the rail's £24,763 and every category percentage with it. Much worse than the bug.

## Consequences

- The Savings node and the summary rail now state the same saving rate, by construction. Verified against the owner's live file: the node reads **56.2%** and the rail reads **"Saving rate: 56.2% of income"** for the same period.
- The reported case resolves from `98%` to `49%`.
- Deficit, income categories, expense categories and the spine are all unchanged — `_denominator_for` reproduces the previous behaviour for every node that was already right.
- Expense-side percentages no longer sum to 100% when a Savings node is present. Deliberate; see above.
- 7 new tests (`tests/test_sankey_savings_percent.py`), including one that pins the specific regression (asserting the old denominator really did yield 98%, so the test can't quietly stop exercising the bug) and one that asserts the diagram label equals the rail's saving rate — the invariant that was violated. Full suite 253/253. No schema change.
