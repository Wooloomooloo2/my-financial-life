"""Generic-CSV date order is inferred from the column, not the row (ADR-148).

A US export dated ``05/12/2021`` used to be read day-first, because
``_parse_generic_date`` tried ``%d/%m/%Y`` before ``%m/%d/%Y`` and returned on
the first pattern that happened to fit. Rows with a day of 13+ failed DMY and
fell through to MDY, so the corruption hit only the ~39% of rows whose day was
12 or less — silently, and invisibly to the row-by-row preview. That put a
$12,000 MS Joint Brokerage transfer on 2021-12-05 instead of 2021-05-12, out of
reach of Reconcile Transfers' ±3-day window.

The fix reads the whole date column first: a field > 12 in position one can
only be a day, in position two can only be a month.

Pure/Qt-free — ``python3 tests/test_csv_date_order.py`` or under pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.import_engine.csv_parser import (
    CsvColumnMapping,
    _parse_generic_date,
    infer_day_first,
    make_generic_date_parser,
    parse_with_mapping,
)


# ── column-level inference ───────────────────────────────────────────────────

def test_infer_month_first_from_day_over_12():
    # 14 can only be a day, and it sits in the second field -> MM/DD.
    assert infer_day_first(["05/12/2021", "05/14/2021"]) is False


def test_infer_day_first_from_day_over_12():
    # 14 can only be a day, and it sits in the first field -> DD/MM.
    assert infer_day_first(["05/12/2021", "14/05/2021"]) is True


def test_infer_none_when_column_never_disambiguates():
    # Every field <= 12: the file genuinely cannot tell us.
    assert infer_day_first(["05/12/2021", "01/02/2021"]) is None


def test_infer_none_when_column_contradicts_itself():
    assert infer_day_first(["14/05/2021", "05/14/2021"]) is None


def test_iso_dates_cast_no_vote():
    assert infer_day_first(["2021-05-12", "2021-05-14"]) is None


def test_dashed_dates_vote():
    assert infer_day_first(["05-12-2021", "14-05-2021"]) is True


def test_junk_rows_are_ignored_not_fatal():
    assert infer_day_first(["", "n/a", "05/14/2021"]) is False


# ── single-cell parsing honours the flag ─────────────────────────────────────

def test_parse_ambiguous_cell_both_ways():
    assert _parse_generic_date("05/12/2021", day_first=False) == "2021-05-12"
    assert _parse_generic_date("05/12/2021", day_first=True) == "2021-12-05"


def test_iso_and_junk_unaffected_by_flag():
    for day_first in (True, False):
        assert _parse_generic_date("2021-05-12", day_first=day_first) == "2021-05-12"
        assert _parse_generic_date("garbage", day_first=day_first) == ""


def test_column_bound_parser_uses_inference():
    parse = make_generic_date_parser(["05/12/2021", "05/14/2021"])
    assert parse("05/12/2021") == "2021-05-12"


def test_undecidable_column_falls_back_to_day_first():
    # Historic behaviour, and the right bias for a UK-origin app.
    parse = make_generic_date_parser(["05/12/2021", "01/02/2021"])
    assert parse("05/12/2021") == "2021-12-05"


# ── the regression, end to end through parse_with_mapping ────────────────────

_US_CSV = (
    "Date,Payee,Amount\n"
    "10/16/2020,MS Joint Checking,10000\n"   # 16 > 12 -> proves MM/DD
    "05/12/2021,eTrade,-12000\n"             # the row that used to flip
    "05/14/2021,Discover IT,-6835.07\n"
)

_UK_CSV = (
    "Date,Payee,Amount\n"
    "15/10/2005,Employer payments,323.33\n"  # 15 > 12 -> proves DD/MM
    "05/12/2021,Employer payments,323.33\n"
)


def _mapping() -> CsvColumnMapping:
    return CsvColumnMapping(
        date_col="Date", date_format="auto", amount_col="Amount",
        amount_inverted=False, debit_col="", credit_col="",
        payee_col="Payee", memo_col="", category_col="",
    )


def test_us_export_keeps_may_12():
    dates = [t["date"] for t in parse_with_mapping(_US_CSV, _mapping())]
    assert dates == ["2020-10-16", "2021-05-12", "2021-05-14"], dates


def test_uk_export_still_reads_day_first():
    dates = [t["date"] for t in parse_with_mapping(_UK_CSV, _mapping())]
    assert dates == ["2005-10-15", "2021-12-05"], dates


def test_one_unambiguous_row_rescues_the_whole_column():
    """The bug's real shape: the ambiguous rows are the majority, and a single
    day-13+ row anywhere in the file is enough to settle the order for all."""
    csv = "Date,Payee,Amount\n" + "".join(
        f"0{m}/0{m}/2021,P,1\n" for m in range(1, 10)
    ) + "05/31/2021,P,1\n"
    dates = [t["date"] for t in parse_with_mapping(csv, _mapping())]
    assert dates[-1] == "2021-05-31"
    assert dates[0] == "2021-01-01"   # unchanged either way, but MM/DD applied
    assert len(dates) == 10


def test_explicit_format_still_overrides_auto():
    m = _mapping()
    m.date_format = "%m/%d/%Y"
    dates = [t["date"] for t in parse_with_mapping(_UK_CSV, m)]
    # 15/10/2005 is invalid MM/DD, so it falls back to the inferred parser
    # (day-first here); 05/12 obeys the explicit format.
    assert dates == ["2005-10-15", "2021-05-12"], dates


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
