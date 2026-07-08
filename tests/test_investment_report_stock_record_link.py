"""Investment-report double-click routes to the Stock Record by column (ADR-144).

Double-clicking a security's **Symbol** or **Security** cell in the Investment
Returns / Investment Income reports opens that security's Stock Record; double-
clicking any other (numeric) cell keeps the pre-existing transactions drill-down
(ADR-083). This guards the column routing so a later table-layout change (e.g.
inserting a column before Symbol/Security) can't silently send the name/symbol
cells back to the transaction list.

The routing lives in ``_on_security_row_activated(row, col)`` and only branches on
``col``; the two destinations are separate helpers. So we exercise the dispatcher
against a stand-in recorder — no DB rows or populated table needed.

Needs PySide6 (importing the windows pulls in QtWidgets); run offscreen under the
miniforge python3:

    QT_QPA_PLATFORM=offscreen \
    /opt/homebrew/Caskroom/miniforge/base/bin/python3 \
    tests/test_investment_report_stock_record_link.py
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
_app.setOrganizationName("MFL")
_app.setApplicationName("MFL")

from mfl_desktop.ui.investment_income_window import InvestmentIncomeWindow
from mfl_desktop.ui.investment_returns_window import InvestmentReturnsWindow


class _Recorder:
    """Stand-in with the two drill helpers replaced by recorders."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def _open_stock_record_for_row(self, row: int) -> None:
        self.calls.append(("stock", row))

    def _open_transactions_for_row(self, row: int) -> None:
        self.calls.append(("txns", row))


def _dispatch(window_cls, row: int, col: int) -> tuple[str, int]:
    rec = _Recorder()
    # Call the real dispatcher with the recorder as ``self`` — it only touches
    # the two helpers and the ``col`` argument.
    window_cls._on_security_row_activated(rec, row, col)
    assert len(rec.calls) == 1, f"expected one drill, got {rec.calls}"
    return rec.calls[0]


def _check(window_cls, label: str) -> None:
    # Symbol (0) and Security (1) → Stock Record.
    assert _dispatch(window_cls, 3, 0) == ("stock", 3), f"{label}: symbol cell"
    assert _dispatch(window_cls, 3, 1) == ("stock", 3), f"{label}: security cell"
    # Every numeric column → transactions drill-down (unchanged, ADR-083).
    for col in range(2, 13):
        assert _dispatch(window_cls, 3, col) == ("txns", 3), \
            f"{label}: numeric col {col}"


def test_returns_report_routes_name_symbol_to_stock_record():
    _check(InvestmentReturnsWindow, "returns")


def test_income_report_routes_name_symbol_to_stock_record():
    _check(InvestmentIncomeWindow, "income")


# ── bare-script runner ──────────────────────────────────────────────────────

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
