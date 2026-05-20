# ===========================================================================
# app/core/accounts/accounts.py
#
# Data layer for account management.
#
# Covers:
#   - Account type definitions (vocabulary mapping)
#   - Creating accounts with opening balance transaction or valuation event
#   - Querying all accounts with computed balances
#   - Balance calculation (transactions for cash/credit; valuations for
#     investment/property)
# ===========================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from pyoxigraph import NamedNode

from app.data.store import store
from app.core.ontology.namespaces import (
    DATA_GRAPH, ONTOLOGY_GRAPH,
    MRL, MRLX, MFL, MFLX,
    RDF_TYPE,
    MRL_CASH_ACCOUNT, MRL_INVESTMENT_ACCOUNT,
    MRL_CREDIT_CARD, MRL_PROPERTY_ASSET,
    MRL_ACCOUNT_NAME, MRL_ACCOUNT_CURRENCY, MRL_ACCOUNT_TYPE,
    MRL_ACCOUNT_NOTES, MRL_OWNED_BY, MRL_IS_LIABILITY,
    MRL_EXCHANGE_RATE, MRL_CURRENCY_CODE, MRL_CURRENCY_SYMBOL,
    MFL_TRANSACTION, MFL_ON_ACCOUNT, MFL_AMOUNT,
    MFL_TRANSACTION_TYPE, MFL_TRANSACTION_STATUS,
    MFL_PAYEE_RAW, MFL_MEMO, MFL_IS_MANUAL_ENTRY,
    MFL_VALUATION_EVENT, MFL_VALUATION_FOR_ACCOUNT,
    MFL_VALUATION_DATE, MFL_VALUATION_AMOUNT,
    MFLX_TYPE_CREDIT, MFLX_TYPE_DEBIT,
    MFLX_STATUS_CLEARED,
    SKOS_PREF_LABEL,
)
from app.core.ontology.iri_factory import (
    next_account_iri, new_transaction_iri, new_valuation_iri, iri_key
)
from app.core.accounts.person import PERSON_IRI

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Account type options
# Maps form keys to RDF classes, vocabulary IRIs, and field families.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountTypeOption:
    key:           str           # Form value, e.g. "cash_current"
    label:         str           # Display label
    rdf_class:     NamedNode     # e.g. mrl:CashAccount
    type_vocab:    Optional[str] # mrlx: IRI string, or None for property
    family:        str           # "cash" | "credit" | "investment" | "property"
    is_liability:  bool = False
    family_label:  str = ""      # Group heading in the accounts list


ACCOUNT_TYPE_OPTIONS: list[AccountTypeOption] = [
    AccountTypeOption("cash_current",   "Current account",          MRL_CASH_ACCOUNT,       MRLX + "CashAccountType_Current",              "cash",       False, "Cash accounts"),
    AccountTypeOption("cash_savings",   "Savings account",          MRL_CASH_ACCOUNT,       MRLX + "CashAccountType_Savings",              "cash",       False, "Cash accounts"),
    AccountTypeOption("cash_fixedterm", "Fixed term deposit",       MRL_CASH_ACCOUNT,       MRLX + "CashAccountType_FixedTerm",            "cash",       False, "Cash accounts"),
    AccountTypeOption("cash_taxadv",    "Tax advantaged cash",      MRL_CASH_ACCOUNT,       MRLX + "CashAccountType_TaxAdvantaged",        "cash",       False, "Cash accounts"),
    AccountTypeOption("credit_std",     "Credit card",              MRL_CREDIT_CARD,        MRLX + "CreditCardAccountType_Standard",       "credit",     True,  "Credit cards"),
    AccountTypeOption("credit_charge",  "Charge card",              MRL_CREDIT_CARD,        MRLX + "CreditCardAccountType_ChargeCard",     "credit",     True,  "Credit cards"),
    AccountTypeOption("inv_stocks",     "Stocks & shares",          MRL_INVESTMENT_ACCOUNT, MRLX + "InvestmentAccountType_StocksShares",   "investment", False, "Investments"),
    AccountTypeOption("inv_taxadv",     "Tax advantaged (ISA/SIPP)",MRL_INVESTMENT_ACCOUNT, MRLX + "InvestmentAccountType_TaxAdvantaged",  "investment", False, "Investments"),
    AccountTypeOption("inv_pension",    "Pension",                  MRL_INVESTMENT_ACCOUNT, MRLX + "InvestmentAccountType_Pension",        "investment", False, "Investments"),
    AccountTypeOption("inv_unittrust",  "Unit trust / fund",        MRL_INVESTMENT_ACCOUNT, MRLX + "InvestmentAccountType_UnitTrust",      "investment", False, "Investments"),
    AccountTypeOption("inv_bonds",      "Bonds",                    MRL_INVESTMENT_ACCOUNT, MRLX + "InvestmentAccountType_Bonds",          "investment", False, "Investments"),
    AccountTypeOption("property",       "Property",                 MRL_PROPERTY_ASSET,     None,                                          "property",   False, "Property"),
]

# Lookup by key
_TYPE_BY_KEY: dict[str, AccountTypeOption] = {o.key: o for o in ACCOUNT_TYPE_OPTIONS}

# Family display order and icons for the accounts list
ACCOUNT_FAMILIES = [
    {"id": "cash",       "label": "Cash accounts", "icon": "ti-cash",        "add_label": "Add cash account"},
    {"id": "credit",     "label": "Credit cards",  "icon": "ti-credit-card", "add_label": "Add credit card"},
    {"id": "investment", "label": "Investments",   "icon": "ti-trending-up", "add_label": "Add investment"},
    {"id": "property",   "label": "Property",      "icon": "ti-home",        "add_label": "Add property"},
]


def get_type_option(key: str) -> Optional[AccountTypeOption]:
    return _TYPE_BY_KEY.get(key)


# ---------------------------------------------------------------------------
# Account summary dataclass — used in the accounts list
# ---------------------------------------------------------------------------

@dataclass
class AccountSummary:
    iri:             NamedNode
    iri_key:         str
    name:            str
    type_key:        str
    type_label:      str
    family:          str
    currency_code:   str
    currency_symbol: str
    balance:         Decimal
    is_liability:    bool


# ---------------------------------------------------------------------------
# Balance queries
# ---------------------------------------------------------------------------

def _sum_transactions(account_iri: NamedNode, type_iri: NamedNode) -> Decimal:
    """Sum all transaction amounts for an account filtered by transaction type."""
    sparql = f"""
        PREFIX mfl:  <{MFL}>
        PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>

        SELECT (COALESCE(SUM(?amount), 0) AS ?total)
        WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?tx a <{MFL_TRANSACTION.value}> ;
                    <{MFL_ON_ACCOUNT.value}>        <{account_iri.value}> ;
                    <{MFL_TRANSACTION_TYPE.value}>  <{type_iri.value}> ;
                    <{MFL_AMOUNT.value}>            ?amount .
            }}
        }}
    """
    for row in store.query(sparql):
        val = row["total"]
        if val is not None:
            return Decimal(str(val.value))
    return Decimal("0")


def get_transaction_balance(account_iri: NamedNode) -> Decimal:
    """
    Balance for cash and credit card accounts.
    Balance = SUM(credits) - SUM(debits)
    For liability accounts (credit cards) this gives the amount owed.
    """
    credits = _sum_transactions(account_iri, MFLX_TYPE_CREDIT)
    debits  = _sum_transactions(account_iri, MFLX_TYPE_DEBIT)
    return credits - debits


def get_valuation_balance(account_iri: NamedNode) -> Decimal:
    """
    Balance for investment and property accounts.
    Returns the most recent ValuationEvent amount, or 0 if none exist.
    """
    sparql = f"""
        PREFIX mfl: <{MFL}>
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>

        SELECT ?amount
        WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?ve a <{MFL_VALUATION_EVENT.value}> ;
                    <{MFL_VALUATION_FOR_ACCOUNT.value}> <{account_iri.value}> ;
                    <{MFL_VALUATION_DATE.value}>         ?date ;
                    <{MFL_VALUATION_AMOUNT.value}>       ?amount .
            }}
        }}
        ORDER BY DESC(?date)
        LIMIT 1
    """
    for row in store.query(sparql):
        val = row["amount"]
        if val is not None:
            return Decimal(str(val.value))
    return Decimal("0")


# ---------------------------------------------------------------------------
# Currency helpers
# ---------------------------------------------------------------------------

def _get_currency_details(currency_iri: NamedNode) -> tuple[str, str]:
    """Return (code, symbol) for a currency IRI."""
    code = symbol = ""
    for q in store.quads_for_pattern(currency_iri, MRL_CURRENCY_CODE, None, ONTOLOGY_GRAPH):
        code = q.object.value
    for q in store.quads_for_pattern(currency_iri, MRL_CURRENCY_SYMBOL, None, ONTOLOGY_GRAPH):
        symbol = q.object.value
    return code, symbol


# ---------------------------------------------------------------------------
# Read — all accounts
# ---------------------------------------------------------------------------

def get_all_accounts() -> dict[str, list[AccountSummary]]:
    """
    Return all accounts grouped by family.
    Keys: "cash", "credit", "investment", "property"
    """
    sparql = f"""
        PREFIX mrl:  <{MRL}>
        PREFIX mrlx: <{MRLX}>

        SELECT ?account ?name ?typeVocab ?currency ?isLiability ?class
        WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?account a ?class ;
                         <{MRL_ACCOUNT_NAME.value}>  ?name ;
                         <{MRL_OWNED_BY.value}>       <{PERSON_IRI.value}> .
                OPTIONAL {{ ?account <{MRL_ACCOUNT_TYPE.value}> ?typeVocab }}
                OPTIONAL {{ ?account <{MRL_ACCOUNT_CURRENCY.value}> ?currency }}
                OPTIONAL {{ ?account <{MRL_IS_LIABILITY.value}> ?isLiability }}
                FILTER(?class IN (
                    mrl:CashAccount,
                    mrl:CreditCardAccount,
                    mrl:InvestmentAccount,
                    mrl:PropertyAsset
                ))
            }}
        }}
        ORDER BY ?name
    """

    # Build a lookup: type_vocab_iri → AccountTypeOption
    vocab_to_option: dict[str, AccountTypeOption] = {
        o.type_vocab: o for o in ACCOUNT_TYPE_OPTIONS if o.type_vocab
    }
    # Also map by rdf_class for property (no vocab)
    class_to_property_option = {
        o.rdf_class.value: o for o in ACCOUNT_TYPE_OPTIONS if o.type_vocab is None
    }

    grouped: dict[str, list[AccountSummary]] = {
        "cash": [], "credit": [], "investment": [], "property": []
    }

    for row in store.query(sparql):
        account_iri  = row["account"]
        name         = row["name"].value
        type_vocab   = row["typeVocab"].value if row["typeVocab"] else None
        currency_iri = row["currency"]
        class_iri    = row["class"].value

        # Resolve type option
        option: Optional[AccountTypeOption] = None
        if type_vocab and type_vocab in vocab_to_option:
            option = vocab_to_option[type_vocab]
        elif class_iri in class_to_property_option:
            option = class_to_property_option[class_iri]

        if not option:
            continue

        # Currency
        code = symbol = ""
        if currency_iri:
            code, symbol = _get_currency_details(currency_iri)

        # Balance
        if option.family in ("cash", "credit"):
            balance = get_transaction_balance(account_iri)
        else:
            balance = get_valuation_balance(account_iri)

        summary = AccountSummary(
            iri=account_iri,
            iri_key=iri_key(account_iri),
            name=name,
            type_key=option.key,
            type_label=option.label,
            family=option.family,
            currency_code=code,
            currency_symbol=symbol,
            balance=balance,
            is_liability=option.is_liability,
        )
        grouped[option.family].append(summary)

    return grouped


# ---------------------------------------------------------------------------
# Write — create account
# ---------------------------------------------------------------------------

def create_account(
    account_type_key:    str,
    account_name:        str,
    currency_iri:        str,
    opening_balance:     Decimal,
    opening_date:        str,          # ISO date string "YYYY-MM-DD"
    notes:               str = "",
    # Cash / credit fields
    interest_rate:       Optional[str] = None,
    credit_limit:        Optional[str] = None,
    statement_day:       Optional[str] = None,
    # Investment fields
    growth_rate:         Optional[str] = None,
    dividend_rate:       Optional[str] = None,
    reinvest_dividends:  bool = False,
    # Property fields
    property_address:    str = "",
    purchase_price:      Optional[str] = None,
    purchase_date:       Optional[str] = None,
    is_mortgaged:        bool = False,
) -> NamedNode:
    """
    Create a new account and its opening balance transaction or valuation.
    Returns the new account IRI.
    """
    option = get_type_option(account_type_key)
    if not option:
        raise ValueError(f"Unknown account type key: {account_type_key}")

    account_iri  = next_account_iri(option.rdf_class)
    currency_node = NamedNode(currency_iri)
    is_liab      = "true" if option.is_liability else "false"
    escaped_name = _esc(account_name)
    escaped_notes= _esc(notes)

    # Build core triples
    triples = f"""
        <{account_iri.value}> a <{option.rdf_class.value}> ;
            <{MRL_ACCOUNT_NAME.value}>  "{escaped_name}"^^<http://www.w3.org/2001/XMLSchema#string> ;
            <{MRL_OWNED_BY.value}>      <{PERSON_IRI.value}> ;
            <{MRL_ACCOUNT_CURRENCY.value}> <{currency_node.value}> ;
            <{MRL_IS_LIABILITY.value}>  "{is_liab}"^^<http://www.w3.org/2001/XMLSchema#boolean> .
    """

    if option.type_vocab:
        triples += f"""
        <{account_iri.value}> <{MRL_ACCOUNT_TYPE.value}> <{option.type_vocab}> .
        """

    if notes.strip():
        triples += f"""
        <{account_iri.value}> <{MRL_ACCOUNT_NOTES.value}> "{escaped_notes}"^^<http://www.w3.org/2001/XMLSchema#string> .
        """

    # Family-specific triples
    if option.family == "cash":
        if interest_rate and interest_rate.strip():
            triples += f"""
        <{account_iri.value}> <{MRL}annualInterestRate> "{interest_rate}"^^<http://www.w3.org/2001/XMLSchema#decimal> .
            """

    elif option.family == "credit":
        if credit_limit and credit_limit.strip():
            triples += f"""
        <{account_iri.value}> <{MRL}creditLimit> "{credit_limit}"^^<http://www.w3.org/2001/XMLSchema#decimal> .
            """
        if statement_day and statement_day.strip():
            triples += f"""
        <{account_iri.value}> <{MRL}statementDay> "{statement_day}"^^<http://www.w3.org/2001/XMLSchema#integer> .
            """

    elif option.family == "investment":
        gr = growth_rate or "0"
        dr = dividend_rate or "0"
        rd = "true" if reinvest_dividends else "false"
        triples += f"""
        <{account_iri.value}> <{MRL}annualGrowthRate>    "{gr}"^^<http://www.w3.org/2001/XMLSchema#decimal> ;
                              <{MRL}annualDividendRate>  "{dr}"^^<http://www.w3.org/2001/XMLSchema#decimal> ;
                              <{MRL}reinvestDividends>   "{rd}"^^<http://www.w3.org/2001/XMLSchema#boolean> .
        """

    elif option.family == "property":
        if property_address.strip():
            triples += f"""
        <{account_iri.value}> <{MRL}propertyAddress> "{_esc(property_address)}"^^<http://www.w3.org/2001/XMLSchema#string> .
            """
        if purchase_price and purchase_price.strip():
            triples += f"""
        <{account_iri.value}> <{MRL}purchasePrice> "{purchase_price}"^^<http://www.w3.org/2001/XMLSchema#decimal> .
            """
        if purchase_date and purchase_date.strip():
            triples += f"""
        <{account_iri.value}> <{MRL}purchaseDate> "{purchase_date}"^^<http://www.w3.org/2001/XMLSchema#date> .
            """
        mortgaged = "true" if is_mortgaged else "false"
        triples += f"""
        <{account_iri.value}> <{MRL}isMortgaged> "{mortgaged}"^^<http://www.w3.org/2001/XMLSchema#boolean> .
        """

    # Insert account triples
    store.update(f"""
        INSERT DATA {{
            GRAPH <{DATA_GRAPH.value}> {{
                {triples}
            }}
        }}
    """)

    logger.info(f"Created account: {account_iri.value} ({option.label})")

    # Create opening balance transaction or valuation event
    amount_str = str(opening_balance) if opening_balance else "0"

    if option.family in ("cash", "credit"):
        _create_opening_transaction(account_iri, amount_str, opening_date)
    else:
        _create_opening_valuation(account_iri, amount_str, opening_date)

    return account_iri


def _create_opening_transaction(
    account_iri: NamedNode,
    amount:      str,
    date_str:    str,
) -> None:
    """Create an opening balance transaction (Credit type) for cash/credit accounts."""
    tx_iri = new_transaction_iri()
    store.update(f"""
        INSERT DATA {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{tx_iri.value}> a <{MFL_TRANSACTION.value}> ;
                    <{MFL_ON_ACCOUNT.value}>         <{account_iri.value}> ;
                    <{MFL}transactionDate>           "{date_str}"^^<http://www.w3.org/2001/XMLSchema#date> ;
                    <{MFL_AMOUNT.value}>             "{amount}"^^<http://www.w3.org/2001/XMLSchema#decimal> ;
                    <{MFL_TRANSACTION_TYPE.value}>   <{MFLX_TYPE_CREDIT.value}> ;
                    <{MFL_TRANSACTION_STATUS.value}> <{MFLX_STATUS_CLEARED.value}> ;
                    <{MFL_PAYEE_RAW.value}>          "Opening Balance"^^<http://www.w3.org/2001/XMLSchema#string> ;
                    <{MFL_MEMO.value}>               "Opening Balance"^^<http://www.w3.org/2001/XMLSchema#string> ;
                    <{MFL_IS_MANUAL_ENTRY.value}>    "true"^^<http://www.w3.org/2001/XMLSchema#boolean> .
            }}
        }}
    """)
    logger.info(f"Created opening balance transaction for {account_iri.value}")


def _create_opening_valuation(
    account_iri: NamedNode,
    amount:      str,
    date_str:    str,
) -> None:
    """Create an opening valuation event for investment/property accounts."""
    ve_iri = new_valuation_iri()
    store.update(f"""
        INSERT DATA {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{ve_iri.value}> a <{MFL_VALUATION_EVENT.value}> ;
                    <{MFL_VALUATION_FOR_ACCOUNT.value}> <{account_iri.value}> ;
                    <{MFL_VALUATION_DATE.value}>         "{date_str}"^^<http://www.w3.org/2001/XMLSchema#date> ;
                    <{MFL_VALUATION_AMOUNT.value}>       "{amount}"^^<http://www.w3.org/2001/XMLSchema#decimal> ;
                    <{MFL}valuationSource>               "Opening valuation"^^<http://www.w3.org/2001/XMLSchema#string> .
            }}
        }}
    """)
    logger.info(f"Created opening valuation for {account_iri.value}")


def _esc(value: str) -> str:
    """Escape double quotes and backslashes for safe SPARQL string insertion."""
    return value.replace("\\", "\\\\").replace('"', '\\"')
