"""Canonical typography scale (ADR-102, P4).

One source of truth for the app's font sizes, so new code reaches for a
named step instead of inventing a pixel value. The app's base ``*`` font is
13 px on macOS / 10 pt on Windows (ADR-076 amendment); these steps are the
QSS/painter accents above and below it.

The scale was reverse-engineered from what the app already used — the sizes
clustered tightly on these nine steps, so adopting it is descriptive, not a
re-layout. Two genuine off-scale one-offs (14 px, 17 px) were folded onto
the nearest step (``LEAD``, ``SUBTITLE``) when this landed.

Sizes are **px** — the app forces Fusion and uses px in QSS for cross-OS
consistency (a *point* size renders ~25 % smaller on macOS; see
``ui_fonts.set_pt`` for the painter-font equivalent).
"""
from __future__ import annotations

# step name → pixel size
MICRO = 10      # fine print, dense axis-adjacent captions
CAPTION = 11    # captions, hints, chips, secondary metadata
SMALL = 12      # secondary labels
BASE = 13       # body (== the default `*` font on macOS)
LEAD = 15       # a slightly-emphasised line (card lead, prominent amount)
SUBTITLE = 18   # section / panel sub-headings
TITLE = 20      # dialog + screen titles
DISPLAY = 22    # large display numbers / hero-adjacent figures
HERO = 30       # the single biggest figure on a screen (e.g. net worth)

SCALE: dict[str, int] = {
    "micro": MICRO, "caption": CAPTION, "small": SMALL, "base": BASE,
    "lead": LEAD, "subtitle": SUBTITLE, "title": TITLE, "display": DISPLAY,
    "hero": HERO,
}


def fs(px: int) -> str:
    """``"font-size: 12px"`` for a scale step — convenience for building QSS
    strings, e.g. ``setStyleSheet(fs(type_scale.LEAD) + "; font-weight: 600;")``."""
    return f"font-size: {px}px"
