"""The categorical series palette (ADR-166).

The old palette was twelve frozen Tailwind hexes whose comment claimed
"tested at AA contrast against white". It was not tested: four colours sat
below 3:1 on white, and **violet-500 vs blue-600 measured ΔE 3.3 under
protanopia** — the same colour to a red-blind reader. The new palette was
produced by iterating against the dataviz validator (OKLCH lightness band,
chroma floor, Machado-2009 CVD separation, contrast vs surface) in all-pairs
mode until every check passed, in *both* themes.

These tests pin the properties the validator checked, so a later "just nudge
that teal" can't quietly reintroduce a collapse. They do NOT re-implement the
validator — they pin the palette's structure, its theme-awareness, and the
no-cycling contract that the eight slots depend on.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from mfl_desktop.ui import tokens
from mfl_desktop.ui.chart_helpers import SERIES_SLOTS, colour_for, series_palette

# The exact hexes the validator signed off. If you change one, re-run:
#   node scripts/validate_palette.js "<hexes>" --mode <mode> --surface <s> --pairs all
_LIGHT = ["#0d9488", "#d99000", "#2a78d6", "#008300",
          "#e34948", "#4a3aa7", "#dd6699", "#eb6834"]
_DARK = ["#0d9488", "#c88400", "#2a78d6", "#008300",
         "#e34948", "#5f4dbf", "#dd6699", "#e3612e"]


def teardown_function():
    tokens.set_theme("light")


def test_the_palette_has_eight_slots():
    tokens.set_theme("light")
    assert SERIES_SLOTS == 8
    assert len(series_palette()) == 8


def test_light_palette_is_the_validated_set():
    tokens.set_theme("light")
    assert [c.lower() for c in series_palette()] == _LIGHT


def test_dark_palette_is_its_own_validated_set():
    """Dark is a *separately validated* set, not an automatic lightening — three
    slots had to move to stay inside the dark lightness band."""
    tokens.set_theme("dark")
    assert [c.lower() for c in series_palette()] == _DARK


def test_the_palette_follows_the_theme():
    tokens.set_theme("light")
    light_indigo = colour_for(5).name().lower()
    tokens.set_theme("dark")
    dark_indigo = colour_for(5).name().lower()
    # Blue (slot 3) and indigo (slot 6) are held apart by *lightness*, not hue —
    # under deuteranopia they are the same hue. The dark surface forces the
    # indigo up, which is exactly why dark needs its own value.
    assert light_indigo == "#4a3aa7"
    assert dark_indigo == "#5f4dbf"
    assert light_indigo != dark_indigo


def test_slot_one_is_the_brand_teal_not_the_accent_itself():
    """The accent (#1f6e78) fails the chroma floor (C 0.075 < 0.10) — as a large
    fill it reads grey. The series slot is the same hue at usable chroma."""
    tokens.set_theme("light")
    assert series_palette()[0].lower() == "#0d9488"
    assert tokens.c("accent").lower() == "#1f6e78"
    assert series_palette()[0].lower() != tokens.c("accent").lower()


def test_colour_is_identity_not_rank():
    """Slot N is always the Nth series. The colour must not depend on how many
    series there are — a filter that drops one must not repaint the survivors."""
    tokens.set_theme("light")
    assert colour_for(2).name().lower() == _LIGHT[2]
    assert colour_for(2).name().lower() == _LIGHT[2]   # stable across calls


def test_the_old_collapsing_pair_is_gone():
    """violet-500 (#8b5cf6) and blue-600 (#2563eb) were ΔE 3.3 apart under
    protanopia — indistinguishable. Neither is in the palette now."""
    tokens.set_theme("light")
    palette = {c.lower() for c in series_palette()}
    assert "#8b5cf6" not in palette
    assert "#2563eb" not in palette


def test_beyond_eight_wraps_and_the_reports_must_fold():
    """The palette does not cycle *by design*, so `colour_for` wrapping past
    slot 8 is a bug the caller must prevent by folding its tail into "Other".
    Pinned so nobody "fixes" the wrap by generating a 9th hue.
    """
    tokens.set_theme("light")
    assert colour_for(8).name().lower() == colour_for(0).name().lower()
