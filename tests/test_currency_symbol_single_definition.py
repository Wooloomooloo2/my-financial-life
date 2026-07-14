"""One definition of a currency glyph, and no unlabelled money (ADR-165).

The UI had grown **twelve** private copies of the currency-symbol table, and
most of them returned an **empty string** for a currency they didn't know — so a
CHF or CAD balance rendered as a bare "1,234.00", indistinguishable from
sterling in a column that also holds sterling. They now all go through
``chart_helpers.currency_symbol()``, which falls back to the code ("CHF 1,234").

The source-scan test is the one that matters: it stops the thirteenth copy.
"""
from __future__ import annotations

import os
import re
import sys
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from mfl_desktop.ui.chart_helpers import currency_symbol
from mfl_desktop.ui.sidebar import Sidebar
from mfl_desktop.ui.transfer_chips import fmt_amount

_UI = _REPO_ROOT / "mfl_desktop" / "ui"

# A dict literal mapping currency codes to glyphs — i.e. a private copy of the
# table. Matches `{"GBP": "£", ...}` regardless of the variable it's bound to.
_SYMBOL_TABLE = re.compile(r'\{\s*"(?:GBP|USD|EUR|JPY)"\s*:\s*"[^"]+"')


def test_only_chart_helpers_defines_the_symbol_table():
    """The regression guard. If this fails, a thirteenth copy has appeared —
    add the currency to `chart_helpers._CCY_SYMBOLS` instead."""
    offenders = [
        p.name
        for p in sorted(_UI.glob("*.py"))
        if p.name != "chart_helpers.py"
        and _SYMBOL_TABLE.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "these modules define their own currency-symbol table; use "
        f"chart_helpers.currency_symbol(): {offenders}"
    )


def test_an_unknown_currency_is_still_labelled():
    # The actual bug: the old tables returned "" here, so the amount printed
    # bare and read as the user's home currency.
    assert currency_symbol("CHF") == "CHF "
    assert currency_symbol("CAD") == "CAD "


def test_a_known_currency_uses_its_glyph():
    assert currency_symbol("GBP") == "£"
    assert currency_symbol("JPY") == "¥"


# ── the surfaces that were wrong ───────────────────────────────────────────

def test_sidebar_labels_an_unknown_currency():
    # Was "1,234.00" — a Swiss-franc account looked like a sterling one.
    assert Sidebar._format(Decimal("1234.00"), "CHF") == "CHF 1,234.00"
    assert Sidebar._format(Decimal("-1234.00"), "CHF") == "-CHF 1,234.00"


def test_sidebar_keeps_the_glyph_for_a_known_currency():
    assert Sidebar._format(Decimal("1040.90"), "GBP") == "£1,040.90"
    assert Sidebar._format(Decimal("17035.62"), "USD") == "$17,035.62"


def test_sidebar_row_with_no_currency_gets_no_symbol():
    # A mixed-currency folder total has no single currency to name — it must not
    # be stamped with a "£" it hasn't earned.
    assert Sidebar._format(Decimal("100.00"), None) == "100.00"


def test_transfer_chip_yen_now_matches_the_rest_of_the_app():
    # transfer_chips' private table was missing JPY entirely, so a yen chip read
    # "JPY 500.00" while the same amount read "¥500.00" everywhere else.
    assert fmt_amount(Decimal("500.00"), "JPY") == "¥500.00"
    assert fmt_amount(Decimal("-500.00"), "JPY") == "-¥500.00"


def test_transfer_chip_keeps_the_code_fallback():
    assert fmt_amount(Decimal("12.34"), "CHF") == "CHF 12.34"
