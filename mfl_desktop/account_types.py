"""Account-type metadata — single source of truth for the five account types.

Each type maps to:
- a short key used as the user-facing short form on the CLI (`cash`, etc.),
- the storage-level type string written to `account.type` (`cash_std`, etc.),
- an MRL class name used to build the account's IRI (`CashAccount`, etc.,
  per ADR-006),
- the family it belongs to (cash / credit / investment / property), and
- whether it is a liability (only credit cards in v1).

The Repository and the UI both import from here so a future new type only
has to be added in one place.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountTypeSpec:
    key: str            # short CLI key, e.g. 'cash'
    storage: str        # account.type column, e.g. 'cash_std'
    label: str          # display label for the type combo
    class_name: str     # MRL class name for IRI generation
    family: str         # 'cash' | 'credit' | 'investment' | 'property'
    is_liability: bool


ACCOUNT_TYPES: tuple[AccountTypeSpec, ...] = (
    AccountTypeSpec("cash",       "cash_std",       "Current account", "CashAccount",       "cash",       False),
    AccountTypeSpec("savings",    "savings_std",    "Savings account", "SavingsAccount",    "cash",       False),
    AccountTypeSpec("credit",     "credit_std",     "Credit card",     "CreditCardAccount", "credit",     True),
    AccountTypeSpec("investment", "investment_std", "Investment",      "InvestmentAccount", "investment", False),
    AccountTypeSpec("property",   "property_std",   "Property",        "PropertyAccount",   "property",   False),
)

_BY_KEY     = {t.key: t for t in ACCOUNT_TYPES}
_BY_STORAGE = {t.storage: t for t in ACCOUNT_TYPES}


def by_key(key: str) -> AccountTypeSpec:
    """Look up by short CLI key (e.g. 'cash'). Raises KeyError if unknown."""
    return _BY_KEY[key]


def by_storage(storage_value: str) -> AccountTypeSpec:
    """Look up by the value stored in `account.type` (e.g. 'cash_std').
    Raises KeyError if unknown."""
    return _BY_STORAGE[storage_value]
