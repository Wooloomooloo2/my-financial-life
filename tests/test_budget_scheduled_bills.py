"""The burn-down projects every scheduled bill in the perimeter (ADR-173).

Owner-reported: scheduled transactions weren't showing in the projection.

They were sourced from a query joining ``budget_line.scheduled_txn_id`` — so
only a schedule someone had *explicitly linked* to an envelope, via ADR-094's
"Make this a bill…", was ever projected. A schedule created the ordinary way
(Manage ▸ Schedules) was invisible.
``mfl_dev.mfl`` is the proof: 5 active schedules, 0 linked, 0 projected.
``mfl_public.mfl`` had 2 linked because its demo builder links them, which is
why the gap never showed up in testing.

Membership is now the **perimeter** — a schedule spends from this budget when
its account is in it, the same rule the actuals obey.

What these lock down:

- An **unlinked** schedule is projected (the bug), stepping on its due day.
- A schedule on an account **outside** the perimeter is not — its spending
  never lands in the actuals either.
- Income/transfer/archived schedules stay out.
- A schedule is **bucketed like its actuals**, so a bill that has been paid is
  recognised as paid and is not counted twice.
- A **group** scope sees its children's bills (an ADR-170 straggler: the scope
  filter was an exact `==`).

    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_budget_scheduled_bills.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import budget_calc as bc
from mfl_desktop.db.repository import PerimeterTxn, Repository

_D = Decimal
_JULY = "2026-07"
_TODAY = date(2026, 7, 10)


def _setup():
    """Two accounts — one in the budget's perimeter, one outside — and a
    category tree Bills ▸ {Cable, Council}, plus Food and Salary."""
    db = Path(tempfile.mkdtemp(prefix="mfl_sched_")) / "m.mfl"
    repo = Repository(db)
    inside = repo.create_account(
        name="Current", type_key="cash", currency="GBP",
        opening_balance=_D("5000.00"),
    )
    outside = repo.create_account(
        name="Car", type_key="cash", currency="GBP",
        opening_balance=_D("1000.00"),
    )
    cats = {}
    cats["bills"] = repo.create_category("Bills", None, "expense")
    cats["cable"] = repo.create_category("Cable", cats["bills"], "expense")
    cats["council"] = repo.create_category(
        "Council Tax", cats["bills"], "expense",
    )
    cats["food"] = repo.create_category("Food", None, "expense")
    cats["salary"] = repo.create_category("Salary", None, "income")

    budget = repo.create_budget(
        name="B", start_month="2026-01", length_months=12,
    )
    repo.set_budget_accounts(budget.id, [(inside.id, "balance")])
    return repo, budget, cats, inside, outside


def _sched(repo, acct, cat, amount, anchor, cadence="monthly"):
    return repo.create_scheduled_txn(
        account_id=acct.id, payee_name="P", category_id=cat,
        transfer_to_account_id=None, estimated_amount=_D(amount),
        variable=False, memo="", cadence=cadence, anchor_date=anchor,
        next_due_date=anchor, end_date=None, auto_post=False, notes="",
    )


def _burn(repo, budget, cats, *, target=None, txns=()):
    occ = bc.bill_occurrences_in_month(
        [
            bc.BillSchedule(
                category_id=d["category_id"], cadence=d["cadence"],
                anchor_date=d["anchor_date"], amount=d["amount"],
                end_date=d["end_date"],
            )
            for d in repo.list_perimeter_schedules(budget.id)
        ],
        _JULY,
    )
    budgeted = {ln.category_id for ln in repo.list_budget_lines(budget.id)}
    return bc.compute_burndown(
        perimeter_txns=list(txns), month=_JULY,
        total_planned=_D("1000.00"), today=_TODAY,
        target_category_id=target,
        parent_map=repo.category_parent_map(),
        budgeted_ids=budgeted, kind_map=repo.category_kind_map(),
        bill_occurrences=occ,
    )


def _txn(tid, day, amount, cat):
    return PerimeterTxn(
        id=tid, account_id=1, posted_date=f"{_JULY}-{day:02d}",
        amount=_D(amount), category_id=cat,
    )


# ── sourcing ─────────────────────────────────────────────────────────────


def test_an_unlinked_schedule_is_projected() -> None:
    """The bug. Nobody ran 'Make this a bill…' on this schedule, and it is
    still a known future outflow."""
    repo, budget, cats, inside, _out = _setup()
    repo.add_budget_line(budget_id=budget.id, category_id=cats["food"])
    _sched(repo, inside, cats["food"], "-90.00", "2026-07-20")

    assert all(
        ln.scheduled_txn_id is None
        for ln in repo.list_budget_lines(budget.id)
    ), "nothing is linked — the old source saw nothing, which was the bug"
    found = repo.list_perimeter_schedules(budget.id)
    assert len(found) == 1
    assert found[0]["category_id"] == cats["food"]

    d = _burn(repo, budget, cats)
    assert d.projected_end == _D("90.00"), "the bill must reach the projection"


def test_the_projection_steps_on_the_due_day() -> None:
    """'Around the date it's due to post' — a bill lands on its own day, not
    smeared across the month as a run-rate."""
    repo, budget, cats, inside, _out = _setup()
    repo.add_budget_line(budget_id=budget.id, category_id=cats["food"])
    _sched(repo, inside, cats["food"], "-90.00", "2026-07-20")

    d = _burn(repo, budget, cats)
    by_day = dict(zip(d.proj_x, d.proj))
    assert by_day[19] == _D("0.00"), "nothing projected before it is due"
    assert by_day[20] == _D("90.00"), "the whole bill steps on day 20"


def test_a_schedule_outside_the_perimeter_is_not_projected() -> None:
    """Its spending never lands in the perimeter's actuals, so projecting it
    would invent an outflow this budget will never see."""
    repo, budget, cats, _inside, outside = _setup()
    repo.add_budget_line(budget_id=budget.id, category_id=cats["food"])
    _sched(repo, outside, cats["food"], "-90.00", "2026-07-20")

    assert repo.list_perimeter_schedules(budget.id) == []
    assert _burn(repo, budget, cats).projected_end == _D("0.00")


def test_an_income_schedule_stays_out() -> None:
    """The chart plots expense outflows and nothing else — its whole-budget
    scope already ignores income actuals, so projecting income would put a
    step in a line that will never move to meet it."""
    repo, budget, cats, inside, _out = _setup()
    _sched(repo, inside, cats["salary"], "2000.00", "2026-07-25")

    assert repo.list_perimeter_schedules(budget.id) == []
    assert _burn(repo, budget, cats).projected_end == _D("0.00")


def test_an_archived_schedule_stays_out() -> None:
    """No repo method archives a schedule yet — the column is only *read*
    (``list_scheduled_txns(include_archived=...)``). The clause mirrors the
    method this replaces, so it is stamped directly here rather than left
    untested until an archive path exists."""
    repo, budget, cats, inside, _out = _setup()
    gone = _sched(repo, inside, cats["food"], "-50.00", "2026-07-12")
    assert len(repo.list_perimeter_schedules(budget.id)) == 1
    repo._conn.execute(
        "UPDATE scheduled_txn SET archived_at = datetime('now') WHERE id = ?",
        (gone,),
    )
    repo.commit()

    assert repo.list_perimeter_schedules(budget.id) == []


# ── bucketing ────────────────────────────────────────────────────────────


def test_a_schedule_buckets_like_its_actuals() -> None:
    """A schedule on 'Cable' under a budgeted 'Bills' must be bucketed to
    Bills — the way its transaction will be. Otherwise the paid bill lands on
    Bills, the occurrence sits on Cable, the amount-match never sees them as
    the same thing, and the bill is counted twice (once actual, once
    projected)."""
    repo, budget, cats, inside, _out = _setup()
    repo.add_budget_line(budget_id=budget.id, category_id=cats["bills"])
    _sched(repo, inside, cats["cable"], "-40.00", "2026-07-05")

    # Already paid, on the 5th, before today.
    d = _burn(repo, budget, cats,
              txns=[_txn(1, 5, "-40.00", cats["cable"])])
    assert d.actual[-1] == _D("40.00")
    assert d.projected_end == _D("40.00"), (
        "the paid bill must not be projected a second time"
    )


def test_an_unpaid_bill_is_still_projected_once() -> None:
    repo, budget, cats, inside, _out = _setup()
    repo.add_budget_line(budget_id=budget.id, category_id=cats["bills"])
    _sched(repo, inside, cats["cable"], "-40.00", "2026-07-25")

    d = _burn(repo, budget, cats)
    assert d.projected_end == _D("40.00")


def test_a_group_scope_sees_its_children_bills() -> None:
    """ADR-170 straggler: the scope filter was an exact `o.category_id ==
    target`, so a group's burn-down — plotted against the group's whole
    roll-up — missed every bill under it."""
    repo, budget, cats, inside, _out = _setup()
    repo.add_budget_line(budget_id=budget.id, category_id=cats["bills"])
    repo.add_budget_line(budget_id=budget.id, category_id=cats["council"])
    # One bill bucketing to the group itself, one to a budgeted child.
    _sched(repo, inside, cats["cable"], "-40.00", "2026-07-22")
    _sched(repo, inside, cats["council"], "-160.00", "2026-07-24")

    d = _burn(repo, budget, cats, target=cats["bills"])
    assert d.projected_end == _D("200.00"), (
        "the group's projection must cover its whole budgeted subtree"
    )

    # ...and a scope on the child alone sees only its own.
    child = _burn(repo, budget, cats, target=cats["council"])
    assert child.projected_end == _D("160.00")


def test_an_unbudgeted_schedule_still_counts_for_the_whole_budget() -> None:
    """Nothing budgeted anywhere near it, but it still spends the perimeter's
    money, so the whole-budget projection must include it (its bucket is
    None — the same bucket its transaction would land in)."""
    repo, budget, cats, inside, _out = _setup()
    _sched(repo, inside, cats["food"], "-75.00", "2026-07-28")

    d = _burn(repo, budget, cats)
    assert d.projected_end == _D("75.00")


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print("\n" + ("all passed" if not failures else f"{failures} failed"))
    sys.exit(1 if failures else 0)
