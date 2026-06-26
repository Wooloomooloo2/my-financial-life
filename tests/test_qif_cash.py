"""QIF cash-ledger parsing — !Type:Bank / !Type:CCard / !Type:Cash / !Type:Oth.

Regression guard for the gap where the QIF parser only handled investment
(!Type:Invst) sections and silently dropped every Bank / Credit-Card
transaction (a 1,202-transaction credit-card export parsed as 0 transactions).

Qt-free: ``qif_parser`` is pure Python. Runs on the base interpreter
(``python3 tests/test_qif_cash.py``) or under pytest.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.import_engine.qif_parser import parse_qif


def _parse(text: str):
    return parse_qif(text.encode("utf-8"), "test.qif")


_CCARD = """!Account
NREI Master Card
TCCard
^
!Type:CCard
D4/12/15
T0.00
Cc
POpening Balance
M
^
D6/2/15
T-55.50
Cc
PShowers Pass
M24692165152000286760905
LPersonal:Clothing
^
D6/15/15
T120.00
Cn
PPayment Received
LTransfer
^
"""


def test_ccard_section_parses_transactions():
    r = _parse(_CCARD)
    assert r.is_investment is False
    assert r.account.name == "REI Master Card"
    assert len(r.transactions) == 3


def test_ccard_signs_and_fields():
    r = _parse(_CCARD)
    charge = r.transactions[1]
    assert charge["date"] == "2015-06-02"
    assert charge["amount"] == Decimal("55.50")
    assert charge["tx_type"] == "debit"            # negative T == cash out
    assert charge["payee_raw"] == "Showers Pass"
    assert charge["category_raw"] == "Personal:Clothing"
    assert charge["status_override"] == "Cleared"  # C c


def test_positive_amount_is_credit():
    r = _parse(_CCARD)
    payment = r.transactions[2]
    assert payment["amount"] == Decimal("120.00")
    assert payment["tx_type"] == "credit"          # positive T == cash in


def test_bank_section_and_transfer_and_check_number():
    text = (
        "!Type:Bank\n"
        "D1/3/24\nT-42.00\nN1234\nPRent\nMmarch\nL[Savings]\n^\n"
    )
    r = _parse(text)
    assert len(r.transactions) == 1
    t = r.transactions[0]
    assert t["amount"] == Decimal("42.00") and t["tx_type"] == "debit"
    # L[Account] is a transfer: category left empty, noted in the memo.
    assert t["category_raw"] == ""
    assert "Transfer to Savings" in t["memo"]
    assert "#1234" in t["memo"]                     # cheque number folded in


def test_split_falls_back_to_first_split_category():
    text = (
        "!Type:CCard\n"
        "D2/2/24\nT-100.00\nPCostco\nSPersonal:Groceries\n$-60.00\n"
        "SPersonal:Household\n$-40.00\n^\n"
    )
    r = _parse(text)
    t = r.transactions[0]
    assert t["amount"] == Decimal("100.00")
    # Not exploded into child rows this round, but not silently uncategorised.
    assert t["category_raw"] == "Personal:Groceries"


def test_investment_section_still_parses():
    """Regression: adding cash sections must not break investment parsing."""
    text = (
        "!Type:Invst\n"
        "D1/5/24\nNBuy\nYAcme Corp\nQ10\nI5.00\nT50.00\n^\n"
    )
    r = _parse(text)
    assert r.is_investment is True
    assert len(r.transactions) == 1
    assert r.transactions[0]["action"] == "Buy"


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
