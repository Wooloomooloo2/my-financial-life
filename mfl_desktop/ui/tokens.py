"""Design tokens + live theming (ADR-076, Arc B).

One source of truth for the app's colours, as **named semantic tokens** each
carrying a light and a dark value. `c(name)` returns the active theme's hex.

Two cooperating mechanisms let the whole app switch theme live (see
`ui/theme.py` for the global palette/QSS half):

- `themed(widget, template)` registers a per-widget stylesheet template (with
  `{token}` placeholders); it's formatted with the active theme now and
  re-formatted on every theme change. Handles arbitrary colour+size+weight
  combos that a fixed CSS-class list couldn't.
- `notifier.changed` is emitted on a theme change so paintEvent charts (which
  read structural colours via `c(...)` at paint time) can `update()`.

**Discipline:** each token's *light* value equals the hex it replaced across
the app, so the light theme is unchanged by construction — only the dark
values are new.
"""
from __future__ import annotations

import logging
import re
from weakref import WeakKeyDictionary

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# ── token tables ──────────────────────────────────────────────────────────────
# name -> (light, dark). Light values reproduce the app's prior hardcoded hex,
# unified onto the Tailwind slate ramp (was a mix of slate + gray).
_TOKENS: dict[str, tuple[str, str]] = {
    # surfaces
    "canvas":          ("#f8fafc", "#0f172a"),  # app/window background
    "surface":         ("#ffffff", "#1e293b"),  # cards, inputs, table base
    "surface_alt":     ("#f1f5f9", "#334155"),  # hover, alternating rows
    "border":          ("#e2e8f0", "#334155"),
    "border_strong":   ("#cbd5e1", "#475569"),
    # text
    "text":            ("#0f172a", "#f1f5f9"),  # primary / strong numbers
    "heading":         ("#334155", "#e2e8f0"),  # section / row headings
    "muted":           ("#64748b", "#94a3b8"),  # secondary labels
    "muted_strong":    ("#475569", "#cbd5e1"),
    "subtle":          ("#94a3b8", "#64748b"),  # faint / placeholder
    "disabled":        ("#9ca3af", "#64748b"),
    # accent — brand teal (ADR-100, re-toned from blue-600 to the app-icon
    # petrol teal). Dark values run a touch brighter for contrast on the dark
    # surface. The accent re-tones the whole token-driven UI (buttons,
    # selection, links, focus, menu highlight) in one place.
    "accent":          ("#1f6e78", "#39a0aa"),
    "accent_hover":    ("#185860", "#2f8893"),
    "accent_subtle":   ("#d8edef", "#1f4248"),  # selection background (muted in dark)
    "on_accent":       ("#ffffff", "#ffffff"),
    # brand gold (ADR-100) — the app icon's mark colour, used sparingly for
    # brand moments (hero numbers, About) — NOT for money (green/red own that).
    "brand_gold":      ("#c9a23a", "#dcbb55"),
    # state
    "positive":        ("#16a34a", "#22c55e"),
    "positive_strong": ("#15803d", "#22c55e"),  # green-700 — strong surplus / income-beat / goal-met
    "negative":        ("#dc2626", "#f87171"),
    "negative_strong": ("#b91c1c", "#ef4444"),
    "caution":         ("#f59e0b", "#fbbf24"),  # amber-500 — near-limit bar fill
    "warning":         ("#b45309", "#fbbf24"),
    # tints (cell-background washes — kept subtle so light text reads on dark)
    "today_col":       ("#eff6ff", "#243352"),  # today's month column (blue-50 light)
    "rollover_bg":     ("#fef9c3", "#3f3a18"),  # carried-in rollover cell (amber-100 light)

    # ── categorical series palette (ADR-166) ─────────────────────────────────
    # The eight chart-series slots, in FIXED order. Identity colours: slot N is
    # always the Nth series, never reassigned by rank or by how many series a
    # filter left behind.
    #
    # These are not eyeballed. Both columns were produced by iterating against
    # the dataviz validator (OKLCH lightness band, chroma floor, Machado-2009
    # CVD separation for protanopia/deuteranopia, and contrast vs the chart
    # surface) until every check passed, in **all-pairs** mode — the standard
    # the sunburst and treemap need, where any two slices can end up touching.
    # The dark column is a *separately validated* set against the dark surface
    # (#1e293b), not an automatic lightening of the light one: three slots had
    # to move to stay inside the dark lightness band.
    #
    # Slot 1 is the brand teal's *chart-safe* sibling. The accent itself
    # (#1f6e78) fails the chroma floor (C 0.075 < 0.10) — as a large fill it
    # reads grey — so the series slot is the same hue at usable chroma.
    #
    # Do not "just tweak" one of these. Re-run:
    #   node scripts/validate_palette.js "<the 8 hexes>" --mode light \
    #        --surface "#ffffff" --pairs all
    # Blue (slot 3) and indigo (slot 6) are held apart by *lightness*, not hue —
    # under deuteranopia they are the same hue, and closing that lightness gap
    # collapses them (this is exactly the defect the old palette had).
    "series_1":        ("#0d9488", "#0d9488"),  # teal — brand
    "series_2":        ("#d99000", "#c88400"),  # amber
    "series_3":        ("#2a78d6", "#2a78d6"),  # blue
    "series_4":        ("#008300", "#008300"),  # green
    "series_5":        ("#e34948", "#e34948"),  # red
    "series_6":        ("#4a3aa7", "#5f4dbf"),  # indigo
    "series_7":        ("#dd6699", "#dd6699"),  # pink
    "series_8":        ("#eb6834", "#e3612e"),  # orange
}

_THEMES = ("light", "dark")
_state = {"theme": "light"}


def current_theme() -> str:
    return _state["theme"]


def c(name: str) -> str:
    """Active theme's hex for a token. Unknown names fall through to a visible
    magenta so a typo is obvious rather than silent."""
    pair = _TOKENS.get(name)
    if pair is None:
        logger.warning("Unknown design token %r", name)
        return "#ff00ff"
    return pair[1 if _state["theme"] == "dark" else 0]


class _Notifier(QObject):
    changed = Signal()


notifier = _Notifier()

# widget -> stylesheet template ("color: {muted};"). WeakKeyDictionary so a
# destroyed widget drops out without us tracking lifetimes.
_registry: "WeakKeyDictionary" = WeakKeyDictionary()


def themed(widget, template: str) -> None:
    """Apply a token-templated stylesheet to ``widget`` and keep it in sync
    with the active theme. ``template`` uses ``{token_name}`` placeholders."""
    _registry[widget] = template
    _apply_one(widget, template)


def _apply_one(widget, template: str) -> bool:
    """Format + set one widget's stylesheet. Returns False if the widget's
    underlying C++ object is gone (so the caller can forget it)."""
    try:
        widget.setStyleSheet(_format(template))
        return True
    except RuntimeError:
        return False


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def _format(template: str) -> str:
    """Substitute ``{token}`` placeholders with the active hex. Uses a regex
    (not ``str.format``) so literal QSS braces — ``QLabel {{ … }}`` blocks,
    ``:hover {{ … }}`` — pass through untouched and don't need escaping."""
    return _PLACEHOLDER.sub(
        lambda m: c(m.group(1)) if m.group(1) in _TOKENS else m.group(0),
        template,
    )


def set_theme(name: str) -> None:
    """Switch the active theme and re-apply every registered widget template.
    Emits ``notifier.changed`` so chart widgets repaint. ``theme.py`` handles
    the global palette/QSS re-apply."""
    if name not in _THEMES:
        name = "light"
    _state["theme"] = name
    dead = [w for w, tpl in list(_registry.items()) if not _apply_one(w, tpl)]
    for w in dead:
        _registry.pop(w, None)
    notifier.changed.emit()
