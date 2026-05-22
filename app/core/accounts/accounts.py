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
#   - Reading and updating mutable account fields (edit form)
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
# Account edit dataclass — used by the edit form
# ---------------------------------------------------------------------------

@dataclass
class AccountEditData:
    """All fields needed to render and process the account edit form."""
    iri_key:            str
    type_key:           str
    type_label:         str
    family:             str
    name:               str
    currency_iri:       str
    currency_code:      str
    currency_symbol:    str
    notes:              str  = ""
    # Cash
    interest_rate:      str  = ""
    # Credit
    credit_limit:       str  = ""
    statement_day:      str  = ""
    # Investment
    growth_rate:        str  = ""
    dividend_rate:      str  = ""
    reinvest_dividends: bool = False
    # Property
    property_address:   str  = ""
    purchase_price:     str  = ""
    purchase_date:      str  = ""
    is_mortgaged:       bool = False


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
# Read — single account for edit form
# ---------------------------------------------------------------------------

def get_account_for_edit(iri_key_str: str) -> Optional[AccountEditData]:
    """
    Read all editable fields for the account edit form.
    Returns None if the account does not exist.
    """
    from app.core.ontology.iri_factory import iri_from_key
    account_iri = iri_from_key(iri_key_str)

    fields: dict = {
        "notes": "", "interest_rate": "", "credit_limit": "",
        "statement_day": "", "growth_rate": "", "dividend_rate": "",
        "reinvest_dividends": False, "property_address": "",
        "purchase_price": "", "purchase_date": "", "is_mortgaged": False,
    }
    type_vocab_iri    = None
    rdf_class_iri     = None
    currency_iri_node = None
    name              = None

    for quad in store.quads_for_pattern(account_iri, None, None, DATA_GRAPH):
        pred = quad.predicate.value
        obj  = quad.object

        if pred == MRL_ACCOUNT_NAME.value:
            name = obj.value
        elif pred == RDF_TYPE.value and obj.value.startswith(MRL):
            rdf_class_iri = obj.value
        elif pred == MRL_ACCOUNT_TYPE.value:
            type_vocab_iri = obj.value
        elif pred == MRL_ACCOUNT_CURRENCY.value:
            currency_iri_node = obj
        elif pred == MRL_ACCOUNT_NOTES.value:
            fields["notes"] = obj.value
        elif pred == MRL + "annualInterestRate":
            fields["interest_rate"] = obj.value
        elif pred == MRL + "creditLimit":
            fields["credit_limit"] = obj.value
        elif pred == MRL + "statementDay":
            fields["statement_day"] = obj.value
        elif pred == MRL + "annualGrowthRate":
            fields["growth_rate"] = obj.value
        elif pred == MRL + "annualDividendRate":
            fields["dividend_rate"] = obj.value
        elif pred == MRL + "reinvestDividends":
            fields["reinvest_dividends"] = obj.value.lower() == "true"
        elif pred == MRL + "propertyAddress":
            fields["property_address"] = obj.value
        elif pred == MRL + "purchasePrice":
            fields["purchase_price"] = obj.value
        elif pred == MRL + "purchaseDate":
            fields["purchase_date"] = obj.value
        elif pred == MRL + "isMortgaged":
            fields["is_mortgaged"] = obj.value.lower() == "true"

    if name is None:
        return None  # Account not found

    # Resolve type option
    option = None
    if type_vocab_iri:
        option = next(
            (o for o in ACCOUNT_TYPE_OPTIONS if o.type_vocab == type_vocab_iri), None
        )
    if not option and rdf_class_iri:
        option = next(
            (o for o in ACCOUNT_TYPE_OPTIONS
             if o.rdf_class.value == rdf_class_iri and o.type_vocab is None),
            None,
        )

    if not option:
        return None

    code = symbol = currency_iri_str = ""
    if currency_iri_node:
        code, symbol = _get_currency_details(currency_iri_node)
        currency_iri_str = currency_iri_node.value

    return AccountEditData(
        iri_key=iri_key_str,
        type_key=option.key,
        type_label=option.label,
        family=option.family,
        name=name,
        currency_iri=currency_iri_str,
        currency_code=code,
        currency_symbol=symbol,
        **fields,
    )


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


def get_account_by_iri_key(iri_key_str: str) -> Optional[AccountSummary]:
    """
    Return an AccountSummary for a single account by its IRI key.
    Used by the transaction register to load account header details.
    Returns None if the account doesn't exist.
    """
    from app.core.ontology.iri_factory import iri_from_key
    account_iri = iri_from_key(iri_key_str)

    class_to_family = {
        MRL_CASH_ACCOUNT.value:       "cash",
        MRL_CREDIT_CARD.value:        "credit",
        MRL_INVESTMENT_ACCOUNT.value: "investment",
        MRL_PROPERTY_ASSET.value:     "property",
    }

    for account_class, family in class_to_family.items():
        if any(True for _ in store.quads_for_pattern(
            account_iri, RDF_TYPE, NamedNode(account_class), DATA_GRAPH
        )):
            name = type_vocab_iri = None
            currency_iri = None
            is_liability = False

            for quad in store.quads_for_pattern(account_iri, None, None, DATA_GRAPH):
                pred = quad.predicate.value
                if pred == MRL_ACCOUNT_NAME.value:
                    name = quad.object.value
                elif pred == MRL_ACCOUNT_TYPE.value:
                    type_vocab_iri = quad.object.value
                elif pred == MRL_ACCOUNT_CURRENCY.value:
                    currency_iri = quad.object
                elif pred == MRL_IS_LIABILITY.value:
                    is_liability = str(quad.object.value).lower() == "true"

            # Resolve type option
            option = None
            if type_vocab_iri:
                option = next((o for o in ACCOUNT_TYPE_OPTIONS if o.type_vocab == type_vocab_iri), None)
            if not option:
                option = next((o for o in ACCOUNT_TYPE_OPTIONS if o.rdf_class.value == account_class and not o.type_vocab), None)

            code = symbol = ""
            if currency_iri:
                code, symbol = _get_currency_details(currency_iri)

            balance = (
                get_transaction_balance(account_iri)
                if family in ("cash", "credit")
                else get_valuation_balance(account_iri)
            )

            return AccountSummary(
                iri=account_iri,
                iri_key=iri_key_str,
                name=name or "",
                type_key=option.key if option else "",
                type_label=option.label if option else "",
                family=family,
                currency_code=code,
                currency_symbol=symbol,
                balance=balance,
                is_liability=is_liability,
            )

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc(value: str) -> str:
    """Escape double quotes and backslashes for safe SPARQL string insertion."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _del(acc: str, pred: str) -> None:
    """Delete all triples for an account predicate."""
    store.update(f"""
        DELETE WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{acc}> <{pred}> ?o .
            }}
        }}
    """)


def _ins_str(acc: str, pred: str, value: str) -> None:
    store.update(f"""
        INSERT DATA {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{acc}> <{pred}> "{_esc(value)}"^^<http://www.w3.org/2001/XMLSchema#string> .
            }}
        }}
    """)


def _ins_typed(acc: str, pred: str, value: str, dtype: str) -> None:
    store.update(f"""
        INSERT DATA {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{acc}> <{pred}> "{value}"^^<http://www.w3.org/2001/XMLSchema#{dtype}> .
            }}
        }}
    """)


# ---------------------------------------------------------------------------
# Write — update account (mutable fields only)
# ---------------------------------------------------------------------------

def update_account(
    iri_key_str:        str,
    name:               str,
    notes:              str  = "",
    # Cash
    interest_rate:      str  = "",
    # Credit
    credit_limit:       str  = "",
    statement_day:      str  = "",
    # Investment
    growth_rate:        str  = "0",
    dividend_rate:      str  = "0",
    reinvest_dividends: bool = False,
    # Property
    property_address:   str  = "",
    purchase_price:     str  = "",
    purchase_date:      str  = "",
    is_mortgaged:       bool = False,
) -> None:
    """
    Update the mutable fields of an account.
    Account type, currency, and opening balance/date are immutable — this
    function does not touch them.
    """
    from app.core.ontology.iri_factory import iri_from_key
    account_iri = iri_from_key(iri_key_str)
    acc = account_iri.value

    # Determine family from the store so we know which type-specific predicates to manage
    family = None
    for quad in store.quads_for_pattern(account_iri, RDF_TYPE, None, DATA_GRAPH):
        obj_iri = quad.object.value
        if obj_iri.startswith(MRL):
            for opt in ACCOUNT_TYPE_OPTIONS:
                if opt.rdf_class.value == obj_iri:
                    family = opt.family
                    break
        if family:
            break

    if not family:
        raise ValueError(f"Account not found: {iri_key_str}")

    # Name — always present, always updated
    _del(acc, MRL_ACCOUNT_NAME.value)
    _ins_str(acc, MRL_ACCOUNT_NAME.value, name.strip() or "Unnamed account")

    # Notes — optional
    _del(acc, MRL_ACCOUNT_NOTES.value)
    if notes.strip():
        _ins_str(acc, MRL_ACCOUNT_NOTES.value, notes.strip())

    # Family-specific predicates
    if family == "cash":
        _del(acc, MRL + "annualInterestRate")
        if interest_rate.strip():
            _ins_typed(acc, MRL + "annualInterestRate", interest_rate.strip(), "decimal")

    elif family == "credit":
        _del(acc, MRL + "creditLimit")
        if credit_limit.strip():
            _ins_typed(acc, MRL + "creditLimit", credit_limit.strip(), "decimal")
        _del(acc, MRL + "statementDay")
        if statement_day.strip():
            _ins_typed(acc, MRL + "statementDay", statement_day.strip(), "integer")

    elif family == "investment":
        _del(acc, MRL + "annualGrowthRate")
        _ins_typed(acc, MRL + "annualGrowthRate", growth_rate.strip() or "0", "decimal")
        _del(acc, MRL + "annualDividendRate")
        _ins_typed(acc, MRL + "annualDividendRate", dividend_rate.strip() or "0", "decimal")
        _del(acc, MRL + "reinvestDividends")
        _ins_typed(acc, MRL + "reinvestDividends", "true" if reinvest_dividends else "false", "boolean")

    elif family == "property":
        _del(acc, MRL + "propertyAddress")
        if property_address.strip():
            _ins_str(acc, MRL + "propertyAddress", property_address.strip())
        _del(acc, MRL + "purchasePrice")
        if purchase_price.strip():
            _ins_typed(acc, MRL + "purchasePrice", purchase_price.strip(), "decimal")
        _del(acc, MRL + "purchaseDate")
        if purchase_date.strip():
            _ins_typed(acc, MRL + "purchaseDate", purchase_date.strip(), "date")
        _del(acc, MRL + "isMortgaged")
        _ins_typed(acc, MRL + "isMortgaged", "true" if is_mortgaged else "false", "boolean")

    logger.info(f"Updated account: {acc}")


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def delete_account(iri_key: str) -> None:
    """
    Permanently delete an account and ALL linked data:
    transactions, valuation events, and the account record itself.
    """
    from app.core.ontology.iri_factory import iri_from_key as _iri_from_key
    account_iri = _iri_from_key(iri_key)

    # Delete all transactions linked to this account
    store.update(f"""
        DELETE WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?tx <{MFL_ON_ACCOUNT.value}> <{account_iri.value}> ;
                    ?p ?o .
            }}
        }}
    """)

    # Delete all valuation events linked to this account
    store.update(f"""
        DELETE WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?v <{MFL_VALUATION_FOR_ACCOUNT.value}> <{account_iri.value}> ;
                   ?p ?o .
            }}
        }}
    """)

    # Delete the account record itself
    store.update(f"""
        DELETE WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{account_iri.value}> ?p ?o .
            }}
        }}
    """)
    logger.info(f"Deleted account and all linked data: {account_iri.value}")
