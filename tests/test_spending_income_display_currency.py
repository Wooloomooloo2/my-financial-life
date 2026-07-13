"""Spending / Income Over Time convert to a display currency (ADR-159).

`spending_aggregates` and `income_aggregates` used to do `SUM(-t.amount)` with no
currency awareness at all, then the chart formatted the result with
`fmt_currency`'s default "£". On a multi-currency file that meant **dollars and
pounds were added 1:1 and the total was stamped with a pound sign** — a wrong
number, not just a wrong label. (The owner's live file is 25 USD accounts + 13
GBP accounts, 34,648 USD transactions: the 2025 income total read 416,906 when
the true figure was 325,410 GBP.)

Pinned here:
  * amounts convert from each ACCOUNT's currency into the display currency;
  * a single-currency file is untouched (no FX lookup, same numbers as before);
  * ADR-129's net-then-clamp happens AFTER conversion, so a refund in one
    currency still offsets spend in another within the same category;
  * money with no rate on file is DROPPED and reported in `unconverted`, never
    silently counted at face value.

Run headless:  QT_QPA_PLATFORM=offscreen python -m pytest tests/test_spending_income_display_currency.py
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

from mfl_desktop.db.repository import Repository

_FROM, _TO = "2026-01-01", "2026-12-31"
_DATE = "2026-06-01"


def _build(*, with_rate: bool = True):
    """A GBP account and a USD account, one income and one expense txn each."""
    tmp = Path(tempfile.mkdtemp(prefix="mfl_ccy_")) / "t.mfl"
    repo = Repository(tmp)

    gbp = repo.create_account(name="UK", type_key="cash", currency="GBP").id
    usd = repo.create_account(name="US", type_key="cash", currency="USD").id
    salary = repo.create_category("Salary", None, "income")
    meals = repo.create_category("Meals", None, "expense")

    if with_rate:
        # 1 USD = 0.50 GBP — a deliberately unmistakable rate, so a converted
        # figure can never be mistaken for an unconverted one. Both directions,
        # so a USD-display test can convert the pounds too.
        repo.upsert_fx_rate(date=_DATE, base="USD", quote="GBP",
                            rate=Decimal("0.50"), source="manual")
        repo.upsert_fx_rate(date=_DATE, base="GBP", quote="USD",
                            rate=Decimal("2.00"), source="manual")

    _tx(repo, account_id=gbp, category_id=salary, amount="100")    # £100 in
    _tx(repo, account_id=gbp, category_id=meals, amount="-40")     # £40 out
    _tx(repo, account_id=usd, category_id=salary, amount="100")    # $100 in
    _tx(repo, account_id=usd, category_id=meals, amount="-40")     # $40 out
    return repo, {"gbp": gbp, "usd": usd, "salary": salary, "meals": meals}


def _tx(repo, *, account_id, category_id, amount):
    repo.insert_transaction(
        account_id=account_id, posted_date=_DATE, amount=Decimal(amount),
        payee_id=None, category_id=category_id, status="reconciled",
        memo="", import_hash=None, import_batch_id=None,
    )


def _total(res, key):
    return sum(r[key] for r in res["rows"])


def _income(repo, ccy):
    return repo.income_aggregates(
        date_from=_FROM, date_to=_TO, granularity="year", display_currency=ccy,
    )


def _spending(repo, ccy):
    return repo.spending_aggregates(
        date_from=_FROM, date_to=_TO, granularity="year", display_currency=ccy,
    )


# ── the bug ─────────────────────────────────────────────────────────────────

def test_income_converts_usd_into_gbp():
    """£100 + ($100 × 0.50) = £150. The bug summed them to £200."""
    repo, _ = _build()
    assert _total(_income(repo, "GBP"), "income_pence") == 15_000


def test_spending_converts_usd_into_gbp():
    """£40 + ($40 × 0.50) = £60. The bug summed them to £80."""
    repo, _ = _build()
    assert _total(_spending(repo, "GBP"), "spending_pence") == 6_000


def test_the_old_behaviour_really_was_wrong():
    """Without a display currency the raw sum is the meaningless 1:1 mix — kept
    as an explicit statement of what the bug produced, so this test can't quietly
    stop exercising it."""
    repo, _ = _build()
    assert _total(_income(repo, None), "income_pence") == 20_000     # £100 + $100
    assert _total(_income(repo, "GBP"), "income_pence") == 15_000    # correct


def test_converting_into_usd_gives_the_mirror_answer():
    """$100 + (£100 / 0.50) = $300."""
    repo, _ = _build()
    assert _total(_income(repo, "USD"), "income_pence") == 30_000


# ── the things that must NOT change ─────────────────────────────────────────

def test_a_single_currency_file_is_unchanged():
    """The overwhelmingly common case must be byte-identical and touch no FX."""
    repo, ids = _build()
    only_gbp = dict(date_from=_FROM, date_to=_TO, granularity="year",
                    account_ids=[ids["gbp"]])
    plain = repo.income_aggregates(**only_gbp)
    displayed = repo.income_aggregates(**only_gbp, display_currency="GBP")
    assert _total(plain, "income_pence") == _total(displayed, "income_pence") == 10_000
    assert displayed["unconverted"] == {}


def test_refund_nets_across_currencies_before_clamping():
    """ADR-129 nets a category over the whole bucket. Converting must happen
    FIRST — netting per-currency would clamp the USD refund away before it could
    offset the GBP spend, and the total would come out too high."""
    repo, ids = _build()
    # A $60 refund on Meals: converts to £30, so Meals nets £40 − £30 = £10
    # (plus the $40 USD spend → £20) = £30 overall.
    _tx(repo, account_id=ids["usd"], category_id=ids["meals"], amount="60")
    res = _spending(repo, "GBP")
    by_cat = {r["category_id"]: r["spending_pence"] for r in res["rows"]}
    # GBP spend £40 + USD net spend ($40 − $60 = −$20 → −£10) = £30.
    assert by_cat[ids["meals"]] == 3_000


# ── missing rates must never be silently counted ────────────────────────────

def test_unconvertible_money_is_dropped_and_reported():
    """No USD→GBP rate on file. The dollars must NOT be counted at face value;
    they're dropped and surfaced so the report can warn."""
    repo, _ = _build(with_rate=False)
    res = _income(repo, "GBP")
    assert _total(res, "income_pence") == 10_000        # the GBP txn only
    assert res["unconverted"] == {"USD": 10_000}        # the dollars, reported


def test_unconverted_is_empty_when_everything_converts():
    repo, _ = _build()
    assert _income(repo, "GBP")["unconverted"] == {}
    assert _spending(repo, "GBP")["unconverted"] == {}


# ── the chart no longer hard-codes the glyph ────────────────────────────────

def test_chart_formats_in_the_display_currency():
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    from mfl_desktop.ui.spending_chart import SpendingChart

    c = SpendingChart()
    c.render(buckets=["2026"], groups=[(1, "Salary")],
             spending={(1, "2026"): 15_000}, avg_pounds=150.0,
             currency_symbol="$")
    assert c._symbol == "$"
    c.resize(1200, 600)
    c.grab()                                   # force a paint
    rect, _legend = c._compute_rects()
    labels = [t for _r, t in c._layout_bar_totals(rect)]
    assert labels and all(t.startswith("$") for t in labels), labels


def test_currency_symbol_helper():
    from mfl_desktop.ui.chart_helpers import currency_symbol
    assert currency_symbol("USD") == "$"
    assert currency_symbol("GBP") == "£"
    assert currency_symbol("EUR") == "€"
    # No glyph on file → the code, so the number is never ambiguous.
    assert currency_symbol("CHF") == "CHF "


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
