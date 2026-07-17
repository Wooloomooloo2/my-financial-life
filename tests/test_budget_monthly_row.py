"""The monthly view's envelope row says what it means (ADR-171).

The row used to read:

    Bills ↻  [bar]  0.00 / 7,553.13 (+6,571.13)   +7,553.13

— four numbers, one of them (the carry) restating what the diff already said,
and a bare signed figure whose meaning depends on the row's *kind*. Worse, a
rollover deficit drives `available` negative and the row became
`32.99 / -158.63 (-158.63)`, which is not a sentence.

What these lock down:

- Two numbers, not four: `£524.99 of £867.00` + a **worded** remainder. The
  carry lives in the tooltip (ADR-124's move for the annual grid).
- A non-positive `available` says what is true — what was spent, and by how
  much it is over — instead of offering a negative budget to be "of".
- Income never reads as a red deficit for the ordinary state of being
  part-way through a month.
- Money carries its **glyph**, not its ISO code: this view was the last
  surface printing `GBP 822.64` (ADR-159 / ADR-165).

Needs PySide6; run offscreen:

    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_budget_monthly_row.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication, QLabel, QWidget

_app = QApplication.instance() or QApplication([])
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.db.repository import Repository
from mfl_desktop.ui import budget_monthly_view as mv
from mfl_desktop.ui.budget_window import BudgetWindow

_D = Decimal


def _row_texts(view) -> dict[str, list[str]]:
    """label -> the row's label texts, for every rendered envelope row."""
    out = {}
    lay = view._list_lay
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is None or w.layout() is None:
            continue
        parts = [
            c.text() for j in range(w.layout().count())
            if isinstance((c := w.layout().itemAt(j).widget()), QLabel)
        ]
        if parts:
            key = parts[0].strip().lstrip("▾▸ ").replace("↻", "").strip()
            out[key] = parts
    return out


def _build(*, rollover="none", spend="-32.99", budget="33.00",
           month="2026-07"):
    db = Path(tempfile.mkdtemp(prefix="mfl_mrow_")) / "m.mfl"
    repo = Repository(db)
    acct = repo.create_account(
        name="Current", type_key="cash", currency="GBP",
        opening_balance=_D("5000.00"),
    )
    cable = repo.create_category("Cable", None, "expense")
    salary = repo.create_category("Salary", None, "income")
    repo.insert_transaction(
        account_id=acct.id, posted_date=f"{month}-05", amount=_D(spend),
        payee_id=None, category_id=cable, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    repo.insert_transaction(
        account_id=acct.id, posted_date=f"{month}-01", amount=_D("2000.00"),
        payee_id=None, category_id=salary, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    budget_obj = repo.create_budget(
        name="B", start_month="2026-01", length_months=12,
    )
    repo.set_budget_accounts(budget_obj.id, [(acct.id, "balance")])
    lid = repo.add_budget_line(
        budget_id=budget_obj.id, category_id=cable, role="bills",
        rollover=rollover,
    )
    repo.set_line_allocation(lid, month, _D(budget), scope="all")
    sid = repo.add_budget_line(
        budget_id=budget_obj.id, category_id=salary, role="discretionary",
    )
    repo.set_line_allocation(sid, month, _D("2500.00"), scope="all")

    win = BudgetWindow(repo)
    view = win._monthly
    view._month = month
    view._render_month()
    return win, view


def test_the_row_is_two_numbers_and_a_worded_remainder() -> None:
    _win, view = _build()
    rows = _row_texts(view)
    _name, amount, remainder = rows["Cable"]
    assert amount == "£32.99 of £33.00"
    assert remainder == "£0.01 left"


def test_an_overspend_says_over_not_a_bare_minus() -> None:
    _win, view = _build(spend="-50.00", budget="33.00")
    rows = _row_texts(view)
    _name, amount, remainder = rows["Cable"]
    assert amount == "£50.00 of £33.00"
    assert remainder == "£17.00 over"


def test_a_negative_available_does_not_offer_a_budget_to_be_of() -> None:
    """A rollover carries an overspend *backwards*, so `available` goes
    negative — and `32.99 / -158.63` is not a sentence. Say what is true."""
    # £10/month accumulating from January, spent nowhere until June — so June
    # opens with £60 available, spends £100, and carries -£40 into July. July's
    # own £10 leaves it at -£30 available before a penny is spent.
    db = Path(tempfile.mkdtemp(prefix="mfl_neg_")) / "m.mfl"
    repo = Repository(db)
    acct = repo.create_account(
        name="Current", type_key="cash", currency="GBP",
        opening_balance=_D("5000.00"),
    )
    cat = repo.create_category("Cable", None, "expense")
    for day, amt in (("2026-06-05", "-100.00"), ("2026-07-05", "-20.00")):
        repo.insert_transaction(
            account_id=acct.id, posted_date=day, amount=_D(amt),
            payee_id=None, category_id=cat, status="cleared", memo="",
            import_hash=None, import_batch_id=None,
        )
    b = repo.create_budget(name="B", start_month="2026-01", length_months=12)
    repo.set_budget_accounts(b.id, [(acct.id, "balance")])
    lid = repo.add_budget_line(
        budget_id=b.id, category_id=cat, role="bills", rollover="accumulate",
    )
    repo.set_line_allocation(lid, "2026-01", _D("10.00"), scope="all")

    win = BudgetWindow(repo)
    view = win._monthly
    view._month = "2026-07"
    view._render_month()
    rows = _row_texts(view)
    _name, amount, remainder = rows["Cable"]

    cell = next(
        r for s in view._matrix.sections for r in s.rows
        if r.label.startswith("Cable")
    ).cells[6]
    assert cell.available < 0, "test needs a carried-in deficit"
    assert amount == "£20.00 spent", f"got {amount!r}"
    assert " of " not in amount, "there is no budget here to be 'of'"
    assert remainder.endswith("over")


def test_the_carry_moved_from_the_row_to_the_tooltip() -> None:
    _win, view = _build(rollover="accumulate", spend="-5.00", budget="10.00",
                        month="2026-07")
    lay = view._list_lay
    amounts = [
        c for i in range(lay.count())
        if (w := lay.itemAt(i).widget()) is not None and w.layout() is not None
        for j in range(w.layout().count())
        if isinstance((c := w.layout().itemAt(j).widget()), QLabel)
        and " of " in c.text()
    ]
    inline = [a for a in amounts if "(" in a.text()]
    assert not inline, f"carry is still inline: {[a.text() for a in inline]}"
    rolled = [a for a in amounts if "rolled over" in (a.toolTip() or "")]
    assert rolled, "the carry should be explained in a tooltip"
    assert "available this month" in rolled[0].toolTip()


def test_income_under_plan_is_not_an_alarm() -> None:
    """Earning less than planned part-way through a month is every month's
    ordinary state — it must not read as a red deficit."""
    _win, view = _build()
    rows = _row_texts(view)
    _name, amount, remainder = rows["Salary"]
    assert amount == "£2,000.00 of £2,500.00"
    assert remainder == "£500.00 to go", f"got {remainder!r}"

    text, ink = mv._remainder("income", _D("-500.00"), "GBP")
    assert ink == mv._muted_ink(), "under-earning is muted, never red"
    text, ink = mv._remainder("income", _D("500.00"), "GBP")
    assert text == "£500.00 above plan"
    assert ink == mv._good_ink()


def test_money_carries_its_glyph_not_its_iso_code() -> None:
    """This view was the last surface printing 'GBP 822.64' (ADR-159/165)."""
    assert mv._money("GBP", _D("822.64")) == "£822.64"
    assert mv._money("USD", _D("822.64")) == "$822.64"
    assert mv._money("GBP", _D("-20.00")) == "-£20.00", "sign outside the glyph"
    # An unknown currency falls back to a spaced code — never an unlabelled
    # number (ADR-165: money is never printed without its unit).
    assert mv._money("XYZ", _D("5.00")) == "XYZ 5.00"

    _win, view = _build()
    pool = view._unalloc.text()
    assert "GBP " not in pool, f"ISO code still in the pool line: {pool!r}"
    assert "£" in pool


def test_a_rebuild_leaves_no_ghost_rows() -> None:
    """Taking a widget out of a layout does not unparent it. Without an
    explicit `setParent(None)` the old rows stay children of the list and keep
    painting at their old geometry until the deferred delete lands, so a
    rebuild draws the new rows *underneath the old ones* — the bottom of the
    list renders as overlapping text. Only visible in a render, never in a
    string assertion, which is why this counts live children."""
    _win, view = _build()
    view._render_month()
    view._render_month()          # rebuild before any deferred delete runs

    live = [
        c for c in view._list.children()
        if isinstance(c, QWidget) and c.parent() is view._list
    ]
    in_layout = view._list_lay.count()
    assert len(live) <= in_layout, (
        f"{len(live) - in_layout} orphaned row(s) still parented to the list "
        f"— they will paint over the rebuilt rows"
    )


def test_only_one_pool_line_is_on_screen() -> None:
    """Both pages carry a Pool / Assigned / Unallocated line, and on Monthly
    they were drawn a centimetre apart with *different* numbers (the window's
    is pinned to today's month, the view's follows its selector) — which reads
    as a contradiction, not as two facts."""
    win, _view = _build()

    win._view.setCurrentIndex(1)          # Monthly
    win.show()
    _app.processEvents()
    assert not win._info_label.isVisible(), (
        "the window's Pool line duplicates the monthly view's"
    )

    win._view.setCurrentIndex(0)          # Annual — it is the only one there
    _app.processEvents()
    assert win._info_label.isVisible()


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
