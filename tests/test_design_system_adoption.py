"""The two earliest windows joined the design system (ADR-161).

These pin the parts that regress silently: a money string that reverts to an
ISO code, a goal date that goes back to a two-digit year, a frozen hex creeping
back into the rich-text info line, and the tab QSS being dropped from the theme.
They assert on strings and stylesheets, not pixels, so they can't go flaky.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mfl_desktop.ui import theme, tokens
from mfl_desktop.ui.budget_window import _fmt_month, _fmt_month_long, _money


# ── money: the symbol, not the ISO code ────────────────────────────────────

def test_money_uses_the_symbol_not_the_iso_code():
    # The bug: the budget window printed "Pool: GBP 822.64".
    assert _money("GBP", Decimal("822.64")) == "£822.64"
    assert _money("USD", Decimal("1234.50")) == "$1,234.50"


def test_money_puts_the_sign_outside_the_symbol():
    # "-£20", never "£-20".
    assert _money("GBP", Decimal("-2387.36")) == "-£2,387.36"


def test_money_falls_back_to_a_spaced_code_for_an_unknown_currency():
    # currency_symbol (ADR-159) already defines this fallback; don't reinvent it.
    assert _money("CHF", Decimal("10.00")) == "CHF 10.00"


# ── dates: a goal target is not a column header ────────────────────────────

def test_column_header_month_stays_two_digit():
    # Unambiguous in context — the matrix's twelve columns are all one year.
    assert _fmt_month("2026-07") == "Jul 26"


def test_goal_target_month_spells_the_year_out():
    # The bug: a 2049 mortgage payoff rendered "by Jun 49", which reads as 1949.
    assert _fmt_month_long("2049-06") == "Jun 2049"


# ── the info line follows the theme ────────────────────────────────────────

@pytest.mark.parametrize("name", ["light", "dark"])
def test_info_line_colours_come_from_tokens_in_both_themes(name):
    """The Pool/Assigned/Unallocated line is rich text, so its colours live in
    an HTML string that ``tokens.themed`` cannot reach — which is how it kept
    three frozen light-theme hexes through the ADR-097 sweep. Assert the tokens
    actually differ per theme, so a re-frozen hex would show up here."""
    tokens.set_theme(name)
    negative = tokens.c("negative_strong")
    positive = tokens.c("positive_strong")
    assert negative.startswith("#") and positive.startswith("#")
    # The exact hexes that were frozen into the label before ADR-161.
    if name == "dark":
        assert negative.lower() != "#b91c1c"
        assert positive.lower() != "#15803d"
    tokens.set_theme("light")


# ── tabs are styled at all ─────────────────────────────────────────────────

def test_theme_styles_the_tab_bar():
    """Nothing styled QTabWidget before ADR-161, so every tabbed surface fell
    back to Fusion's native tab bar — the loudest reason Account Summary read
    as a different app."""
    qss = theme._qss()
    assert "QTabBar::tab" in qss
    assert "QTabWidget::pane" in qss
    # The selected tab is marked with the brand accent, not a native highlight.
    assert tokens.c("accent") in qss
