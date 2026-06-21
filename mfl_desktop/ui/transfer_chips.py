"""Shared transfer-strength chips + amount formatting (ADR-096, P3b).

Both transfer-matching surfaces — the inline confirm/picker dialogs
(``transfer_match_dialogs.py``, ADR-036) and the bulk Reconcile Transfers
dialog (``transfer_reconcile_dialog.py``, ADR-037) — render the same
three-bucket *strength chip* (Strong / Good / Possible) and the same
currency amount string. Before ADR-096 each file carried its own private
copy of ``_CHIP_COLOURS`` / ``_fmt_amount`` / a strength-chip builder, so
the two could silently drift (one gets a new colour or a currency symbol
the other doesn't). This module is the single source of truth; the row
*layouts* in each dialog stay bespoke.

The chip is a coloured pill with white text — readable in both light and
dark themes by construction (ADR-076), so the colours are literal here
rather than theme tokens; the pill never needs to invert.
"""
from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QWidget,
)


# Strength bucket → pill colour. Match the slate / blue palette established
# by ADR-026's theme: Strong is the affirmative blue, Good is amber,
# Possible is muted slate ("weakest of the three"). The scorer in
# ``transfer_reconcile.py`` only ever emits these three labels; an unknown
# label falls back to the muted slate.
CHIP_COLOURS: dict[str, str] = {
    "Strong":   "#2563EB",   # blue-600
    "Good":     "#F59E0B",   # amber-500
    "Possible": "#64748B",   # slate-500
}

_FALLBACK_COLOUR = "#64748B"

# Currency code → symbol for the amount formatter. Codes outside this map
# render as "EUR 12.34" (code + space + magnitude) so nothing is silently
# mislabelled.
_CURRENCY_SYMBOLS: dict[str, str] = {"GBP": "£", "USD": "$", "EUR": "€"}


def fmt_amount(value: Decimal, currency: str) -> str:
    """``£500.00`` / ``$1,000.00`` / fallback ``EUR 12.34``.

    Sign-aware (leading ``-`` on negatives), thousands-grouped, two
    decimals. Used by both transfer surfaces for every amount cell.
    """
    sym = _CURRENCY_SYMBOLS.get(currency, "")
    sign = "-" if value < 0 else ""
    body = f"{abs(value):,.2f}"
    if sym:
        return f"{sign}{sym}{body}"
    return f"{sign}{currency} {body}"


def _chip_qss(colour: str) -> str:
    return (
        f"QLabel {{"
        f"  color: white; background: {colour}; "
        f"  border-radius: 8px; padding: 2px 8px; "
        f"  font-weight: 600; font-size: 11px;"
        f"}}"
    )


def strength_chip(strength: str) -> QLabel:
    """Small inline pill ``QLabel`` displaying the strength bucket.

    Sized to its content (``Maximum``/``Fixed``) so it hugs the text.
    Drop it straight into a layout, or wrap it with
    :func:`strength_chip_holder` for a centred table-cell widget.
    """
    chip = QLabel(strength)
    chip.setAlignment(Qt.AlignCenter)
    chip.setStyleSheet(_chip_qss(CHIP_COLOURS.get(strength, _FALLBACK_COLOUR)))
    chip.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return chip


def strength_chip_holder(strength: str) -> QWidget:
    """A table-cell-friendly wrapper: the chip left-aligned inside a
    widget with a small margin and a trailing stretch, so it doesn't
    fill the whole cell. Used for ``setCellWidget`` in the picker and
    reconcile tables."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(4, 2, 4, 2)
    layout.addWidget(strength_chip(strength))
    layout.addStretch(1)
    return holder
