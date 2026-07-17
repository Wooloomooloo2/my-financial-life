"""No dialog freezes a theme colour, and the rest can only get better (ADR-167).

ADR-076 built a token layer with a light *and* a dark value for every colour,
and ADR-097 declared the dark-mode sweep complete. It was not. The dialogs
audit found **73** frozen light-theme hex literals across 20 modules, and at
least three of them were user-visible breakage — the reconcile wizard's only
instruction line was `#0F172A` ink on the `#0f172a` dark canvas, i.e. the same
colour as the background: **invisible**.

Two tests:

* **Dialogs are clean, and must stay clean** — zero frozen colours. This is the
  part of the app the audit swept, so it's a hard zero.
* **The rest is a ratchet** — the remaining count may only ever go *down*. The
  chart series/semantic colours (income-green, spend-red, …) are readable but
  untuned in dark; converting them is a separate arc. Until then this stops the
  count growing.

A hex in a *docstring* is prose, not a colour — excluded (which is why this
scans the AST rather than grepping).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_UI = _REPO_ROOT / "mfl_desktop" / "ui"
_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")

# tokens.py *is* the colour table — it is the one place a hex belongs.
_TOKEN_TABLE = "tokens.py"

# Every module that still carries frozen colours, with its current count.
# These are chart series / semantic colours (income-green, spend-red, the
# net-worth family hues, …): readable in dark, but not theme-tuned. Converting
# them is the follow-up arc filed in the backlog under ADR-167.
#
# THIS TABLE MAY ONLY SHRINK. If a number goes up, or a new module appears, the
# test fails — add the colour to tokens.py instead of freezing a new one.
_ALLOWED: dict[str, int] = {
    "net_worth_window.py": 11,
    "returns_chart.py": 7,
    "burn_down_chart.py": 5,
    "sankey_report_window.py": 5,
    "splash.py": 4,
    "transfer_chips.py": 4,
    # ADR-171 converted this view's last three (`_MUTED` / `_GREEN_TXT` /
    # `_RED_TXT`) while redesigning its rows. Zero, and it stays zero.
    "budget_monthly_view.py": 0,
    "proportional_bar.py": 3,
    "value_chart.py": 3,
    "balance_flow_chart.py": 2,
    "income_expense_chart.py": 2,
    "income_expense_window.py": 2,
    "loan_schedule_view.py": 2,
    "register_model.py": 2,
    "sankey_chart.py": 2,
    # Legitimate: white text drawn *on* a coloured fill, and Qt's BrightText
    # role. These are not theme-dependent — white on a saturated bar is white
    # in both themes.
    "theme.py": 1,
    "treemap_chart.py": 1,
}

# Everything that renders a dialog. These were swept; they must stay at zero.
_DIALOG_SUFFIXES = ("_dialog.py", "_wizard.py", "_dialogs.py", "_popover.py")


def _frozen_hexes(path: Path) -> list[tuple[int, str]]:
    """Hex colour literals in real code strings. Docstrings are prose, not
    colour, and are excluded — hence the AST walk rather than a grep."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            hits += [(node.lineno, h) for h in _HEX.findall(node.value)]
    return hits


def _ui_modules() -> list[Path]:
    return [p for p in sorted(_UI.glob("*.py")) if p.name != _TOKEN_TABLE]


def test_no_dialog_freezes_a_theme_colour():
    """The audit's hard result. A frozen light-theme hex in a dialog is how the
    reconcile wizard's instruction line ended up invisible in dark mode."""
    offenders = {}
    for p in _ui_modules():
        if not p.name.endswith(_DIALOG_SUFFIXES):
            continue
        hits = _frozen_hexes(p)
        if hits:
            offenders[p.name] = hits
    assert offenders == {}, (
        "dialogs must take their colours from tokens (a frozen hex cannot "
        f"follow the dark theme): {offenders}"
    )


def test_frozen_colour_count_never_grows():
    """The ratchet. Add a colour to tokens.py; never freeze a new one."""
    grew, appeared = [], []
    for p in _ui_modules():
        n = len(_frozen_hexes(p))
        if n == 0:
            continue
        allowed = _ALLOWED.get(p.name)
        if allowed is None:
            appeared.append((p.name, n))
        elif n > allowed:
            grew.append((p.name, allowed, n))
    assert not appeared, (
        f"new module(s) freezing theme colours — use tokens.py: {appeared}"
    )
    assert not grew, (
        "frozen-colour count grew (allowed → actual); this table may only "
        f"shrink: {grew}"
    )


def test_the_ratchet_table_is_not_stale():
    """If a module was cleaned up, tighten the table — otherwise the ratchet
    quietly stops ratcheting."""
    loose = []
    for name, allowed in _ALLOWED.items():
        p = _UI / name
        if not p.exists():
            loose.append((name, "module gone"))
            continue
        n = len(_frozen_hexes(p))
        if n < allowed:
            loose.append((name, f"allowed {allowed}, actually {n} — lower it"))
    assert not loose, f"stale entries in the ratchet table: {loose}"
