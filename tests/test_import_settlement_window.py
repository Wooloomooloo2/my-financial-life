"""Cross-source dedup tolerates real settlement gaps (ADR-151).

The ±2-day window (ADR-085 original) missed the everyday "posted Friday, cleared
Monday" case — a 3-day gap — so obvious duplicates imported silently as new. The
window is now ±4 days for a confident match, with a weak 'possible' tier out to
±10 for stragglers (exact amount + payee overlap required).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.import_engine import dedupe


def _imp(i, date, pence, payee):
    return dedupe.ImportRow(index=i, date_iso=date, amount_pence=pence, payee_raw=payee)


def _exist(id_, date, pence, payee, manual=True):
    return dedupe.ExistingRow(id=id_, date_iso=date, amount_pence=pence,
                              payee_name=payee, is_manual=manual)


def _match(imports, existing):
    return dedupe.match_duplicates(
        imports, existing,
        window_days=dedupe.DEFAULT_WINDOW_DAYS,
        possible_window_days=dedupe.POSSIBLE_WINDOW_DAYS,
    )


def test_three_day_gap_now_matches_the_manual_toll():
    # The real case: hand-typed "M6 Toll" on Fri, bank line on Mon — 3 days.
    m = _match(
        [_imp(0, "2026-07-06", -1160, "M6 TOLL")],
        [_exist(39026, "2026-07-03", -1160, "M6 Toll Lichfield", manual=True)],
    )
    assert 0 in m, "a 3-day-apart manual match must no longer slip through"
    assert m[0].is_manual is True
    assert m[0].strength == "strong"      # is_manual + payee overlap, in window


def test_three_day_gap_transfer_leg_matches():
    # The Capital One transfer leg, 3 days from the imported DD line.
    m = _match(
        [_imp(0, "2026-07-06", -58838, "CAPITAL ONE")],
        [_exist(21447, "2026-07-09", -58838, "Transfer to Capital One", manual=True)],
    )
    assert 0 in m and m[0].strength == "strong"


def test_two_day_gap_still_matches():
    m = _match(
        [_imp(0, "2026-07-06", -500, "Tesco")],
        [_exist(1, "2026-07-04", -500, "TESCO STORES", manual=True)],
    )
    assert 0 in m and m[0].strength == "strong"


def test_seven_day_straggler_is_a_weak_possible():
    # Beyond the confident window but exact amount + payee overlap → weak.
    m = _match(
        [_imp(0, "2026-07-14", -2000, "British Gas")],
        [_exist(1, "2026-07-07", -2000, "BRITISH GAS", manual=False)],
    )
    assert 0 in m and m[0].strength == "weak"


def test_straggler_without_payee_overlap_is_not_matched():
    # Extended tier REQUIRES payee overlap — an unrelated same-amount charge a
    # week later is genuinely new, not a duplicate.
    m = _match(
        [_imp(0, "2026-07-14", -2000, "Screwfix")],
        [_exist(1, "2026-07-07", -2000, "British Gas", manual=False)],
    )
    assert 0 not in m


def test_beyond_possible_window_is_new():
    # 12 days apart, even with payee overlap, is past the weak tier.
    m = _match(
        [_imp(0, "2026-07-19", -2000, "British Gas")],
        [_exist(1, "2026-07-07", -2000, "BRITISH GAS", manual=False)],
    )
    assert 0 not in m


def test_repeats_still_consume_one_for_one():
    # Two identical charges on file, three incoming — only the two on file are
    # matched (multiplicity preserved); the third is genuinely new.
    imports = [
        _imp(0, "2026-07-06", -890, "ScotRail"),
        _imp(1, "2026-07-06", -890, "ScotRail"),
        _imp(2, "2026-07-06", -890, "ScotRail"),
    ]
    existing = [
        _exist(1, "2026-07-05", -890, "SCOTRAIL", manual=False),
        _exist(2, "2026-07-06", -890, "SCOTRAIL", manual=False),
    ]
    m = _match(imports, existing)
    assert len(m) == 2                    # exactly two claimed, one left new
