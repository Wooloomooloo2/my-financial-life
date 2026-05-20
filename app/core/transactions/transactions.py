# ===========================================================================
# app/core/transactions/transactions.py
#
# Data layer for the transaction register.
# ===========================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pyoxigraph import NamedNode

from app.data.store import store
from app.core.ontology.namespaces import (
    DATA_GRAPH, ONTOLOGY_GRAPH,
    MRL, MRLX, MFL, MFLX,
    RDF_TYPE,
    MRL_ACCOUNT_NAME, MRL_ACCOUNT_TYPE, MRL_ACCOUNT_CURRENCY,
    MRL_IS_LIABILITY,
    MFL_TRANSACTION, MFL_ON_ACCOUNT, MFL_AMOUNT,
    MFL_TRANSACTION_TYPE, MFL_TRANSACTION_STATUS,
    MFL_PAYEE_RAW, MFL_MEMO, MFL_NOTES, MFL_CATEGORY,
    MFL_IS_MANUAL_ENTRY,
    MFLX_TYPE_CREDIT, MFLX_TYPE_DEBIT,
    MFLX_STATUS_CLEARED,
)
from app.core.ontology.iri_factory import iri_from_key, mfl_iri_from_key
from app.core.accounts.accounts import (
    get_transaction_balance,
    get_valuation_balance,
    _get_currency_details,
    ACCOUNT_TYPE_OPTIONS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CATEGORY_SCHEME_IRI = MFLX + "TransactionCategoryScheme"
INCOME_TOP          = MFLX + "TransactionCategory_Income"
EXPENSE_TOP         = MFLX + "TransactionCategory_Expense"
UNCAT_IRI           = MFLX + "TransactionCategory_Uncategorised"

STATUS_META: dict[str, tuple[str, str]] = {
    MFLX + "TransactionStatus_Pending":    ("Pending",    "badge-warning"),
    MFLX + "TransactionStatus_Uncleared":  ("Uncleared",  "badge-ghost"),
    MFLX + "TransactionStatus_Cleared":    ("Cleared",    "badge-success"),
    MFLX + "TransactionStatus_Reconciled": ("Reconciled", "badge-info"),
}

STATUS_OPTIONS = [
    (MFLX + "TransactionStatus_Pending",    "Pending"),
    (MFLX + "TransactionStatus_Uncleared",  "Uncleared"),
    (MFLX + "TransactionStatus_Cleared",    "Cleared"),
    (MFLX + "TransactionStatus_Reconciled", "Reconciled"),
]

_FIELD_PREDICATES = {
    "category": MFL_CATEGORY.value,
    "status":   MFL_TRANSACTION_STATUS.value,
    "payee":    MFL_PAYEE_RAW.value,
    "memo":     MFL_NOTES.value,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AccountDetail:
    iri:             NamedNode
    iri_key:         str
    name:            str
    type_label:      str
    family:          str
    currency_code:   str
    currency_symbol: str
    balance:         Decimal
    is_liability:    bool


@dataclass
class CategoryItem:
    iri:   str
    label: str


@dataclass
class CategoryGroup:
    label: str
    items: list[CategoryItem]


@dataclass
class TransactionRow:
    iri:                     NamedNode
    iri_key:                 str
    date_iso:                str
    date_display:            str
    payee_display:           str
    memo:                    str
    notes:                   str
    category_iri:            str
    category_label:          str
    category_color:          str
    status_iri:              str
    status_label:            str
    status_badge:            str
    tx_type:                 str
    amount:                  Decimal
    amount_display:          str
    amount_color:            str
    running_balance:         Decimal
    running_balance_display: str
    running_balance_color:   str
    is_manual:               bool


# ---------------------------------------------------------------------------
# Account detail
# ---------------------------------------------------------------------------

def get_account_detail(iri_key_str: str) -> Optional[AccountDetail]:
    account_iri   = iri_from_key(iri_key_str)
    name          = None
    type_vocab    = None
    currency_iri  = None
    is_liability  = False
    rdf_class_val = None

    for quad in store.quads_for_pattern(account_iri, None, None, DATA_GRAPH):
        pred = quad.predicate.value
        obj  = quad.object
        if pred == MRL_ACCOUNT_NAME.value:
            name = obj.value
        elif pred == MRL_ACCOUNT_TYPE.value:
            type_vocab = obj.value
        elif pred == MRL_ACCOUNT_CURRENCY.value:
            currency_iri = obj
        elif pred == MRL_IS_LIABILITY.value:
            is_liability = obj.value.lower() == "true"
        elif pred == RDF_TYPE.value and obj.value.startswith(MRL):
            rdf_class_val = obj.value

    if not name:
        return None

    option = None
    for opt in ACCOUNT_TYPE_OPTIONS:
        if type_vocab and opt.type_vocab == type_vocab:
            option = opt
            break
        if not type_vocab and opt.type_vocab is None and opt.rdf_class.value == rdf_class_val:
            option = opt
            break

    type_label = option.label  if option else "Account"
    family     = option.family if option else "cash"
    code = symbol = ""
    if currency_iri:
        code, symbol = _get_currency_details(currency_iri)

    balance = (
        get_transaction_balance(account_iri)
        if family in ("cash", "credit")
        else get_valuation_balance(account_iri)
    )

    return AccountDetail(
        iri=account_iri, iri_key=iri_key_str, name=name,
        type_label=type_label, family=family,
        currency_code=code, currency_symbol=symbol,
        balance=balance, is_liability=is_liability,
    )


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def _fmt_date(iso: str) -> str:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return f"{d.day} {d.strftime('%b %Y')}"
    except ValueError:
        return iso


def get_transactions_for_account(account_detail: AccountDetail) -> list[TransactionRow]:
    sparql = f"""
        SELECT ?tx ?date ?amount ?txType ?status
               ?payeeRaw ?memo ?notes ?category ?isManual
        WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                ?tx a <{MFL_TRANSACTION.value}> ;
                    <{MFL_ON_ACCOUNT.value}>        <{account_detail.iri.value}> ;
                    <{MFL}transactionDate>           ?date ;
                    <{MFL_AMOUNT.value}>             ?amount ;
                    <{MFL_TRANSACTION_TYPE.value}>   ?txType ;
                    <{MFL_TRANSACTION_STATUS.value}> ?status .
                OPTIONAL {{ ?tx <{MFL_PAYEE_RAW.value}> ?payeeRaw }}
                OPTIONAL {{ ?tx <{MFL_MEMO.value}>       ?memo }}
                OPTIONAL {{ ?tx <{MFL_NOTES.value}>      ?notes }}
                OPTIONAL {{ ?tx <{MFL_CATEGORY.value}>   ?category }}
                OPTIONAL {{ ?tx <{MFL_IS_MANUAL_ENTRY.value}> ?isManual }}
            }}
        }}
        ORDER BY ASC(?date) ASC(STR(?tx))
    """

    cat_labels   = _load_category_labels()
    cat_families = _load_category_families()
    sym          = account_detail.currency_symbol

    running = Decimal("0")
    rows: list[TransactionRow] = []

    for row in store.query(sparql):
        tx_iri   = row["tx"]
        key      = tx_iri.value.split("#")[-1]
        date_iso = row["date"].value
        amount   = Decimal(str(row["amount"].value))
        tx_type  = row["txType"].value.split("#")[-1]
        status   = row["status"].value

        payee_raw = row["payeeRaw"].value if row["payeeRaw"] else ""
        memo      = row["memo"].value     if row["memo"]     else ""
        notes_val = row["notes"].value    if row["notes"]    else ""
        cat_iri   = row["category"].value if row["category"] else ""
        is_manual = (row["isManual"] is not None
                     and row["isManual"].value.lower() == "true")

        is_debit = "Debit" in tx_type

        if is_debit:
            running -= amount
            amt_str   = f"−{sym}{amount:,.2f}"
            amt_color = "text-error"
        else:
            running += amount
            amt_str   = f"{sym}{amount:,.2f}"
            amt_color = "text-base-content"

        bal_str   = (f"−{sym}{abs(running):,.2f}" if running < 0
                     else f"{sym}{running:,.2f}")
        bal_color = "text-error" if running < 0 else "text-base-content/60"

        cat_label = cat_labels.get(cat_iri, "Uncategorised") if cat_iri else "Uncategorised"
        cat_fam   = cat_families.get(cat_iri, "uncat")        if cat_iri else "uncat"
        cat_color = {
            "income":  "text-success text-xs",
            "expense": "text-base-content text-xs",
            "uncat":   "text-base-content/30 text-xs italic",
        }.get(cat_fam, "text-base-content/30 text-xs italic")

        s_label, s_badge = STATUS_META.get(status, ("Unknown", "badge-ghost"))

        rows.append(TransactionRow(
            iri=tx_iri, iri_key=key,
            date_iso=date_iso, date_display=_fmt_date(date_iso),
            payee_display=payee_raw or memo or "—",
            memo=memo, notes=notes_val,
            category_iri=cat_iri, category_label=cat_label, category_color=cat_color,
            status_iri=status, status_label=s_label, status_badge=s_badge,
            tx_type=tx_type, amount=amount,
            amount_display=amt_str, amount_color=amt_color,
            running_balance=running,
            running_balance_display=bal_str, running_balance_color=bal_color,
            is_manual=is_manual,
        ))

    rows.reverse()
    return rows


# ---------------------------------------------------------------------------
# Categories for select dropdowns
# ---------------------------------------------------------------------------

def _load_category_labels() -> dict[str, str]:
    sparql = f"""
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        SELECT ?concept ?label
        WHERE {{
            GRAPH <{ONTOLOGY_GRAPH.value}> {{
                ?concept skos:inScheme <{CATEGORY_SCHEME_IRI}> ;
                         skos:prefLabel ?label .
                FILTER(LANG(?label) = "en")
            }}
        }}
    """
    return {r["concept"].value: r["label"].value for r in store.query(sparql)}


def _load_category_families() -> dict[str, str]:
    sparql = f"""
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        SELECT ?concept ?broader
        WHERE {{
            GRAPH <{ONTOLOGY_GRAPH.value}> {{
                ?concept skos:inScheme <{CATEGORY_SCHEME_IRI}> .
                OPTIONAL {{ ?concept skos:broader ?broader }}
            }}
        }}
    """
    families: dict[str, str] = {}
    for row in store.query(sparql):
        concept = row["concept"].value
        broader = row["broader"].value if row["broader"] else None
        if concept == UNCAT_IRI:
            families[concept] = "uncat"
        elif broader == INCOME_TOP or concept == INCOME_TOP:
            families[concept] = "income"
        elif broader == EXPENSE_TOP or concept == EXPENSE_TOP:
            families[concept] = "expense"
        else:
            families[concept] = "uncat"
    return families


def get_categories_for_select() -> list[CategoryGroup]:
    sparql = f"""
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        SELECT ?concept ?label ?broader
        WHERE {{
            GRAPH <{ONTOLOGY_GRAPH.value}> {{
                ?concept skos:inScheme <{CATEGORY_SCHEME_IRI}> ;
                         skos:prefLabel ?label .
                OPTIONAL {{ ?concept skos:broader ?broader }}
            }}
        }}
        ORDER BY ?broader ?label
    """
    # Collect all results, preferring @en labels
    concepts: dict[str, dict] = {}
    for row in store.query(sparql):
        concept = row["concept"].value
        label   = row["label"].value
        lang    = getattr(row["label"], "language", "") or ""
        broader = row["broader"].value if row["broader"] else None

        # Store concept, preferring English labels over others
        if concept not in concepts or lang == "en":
            concepts[concept] = {"label": label, "broader": broader}

    income_items:  list[CategoryItem] = []
    expense_items: list[CategoryItem] = []

    for concept, data in concepts.items():
        label   = data["label"]
        broader = data["broader"]
        if concept in (INCOME_TOP, EXPENSE_TOP, UNCAT_IRI):
            continue
        if broader == INCOME_TOP:
            income_items.append(CategoryItem(iri=concept, label=label))
        elif broader == EXPENSE_TOP:
            expense_items.append(CategoryItem(iri=concept, label=label))

    income_items.sort(key=lambda x: x.label)
    expense_items.sort(key=lambda x: x.label)

    return [
        CategoryGroup(label="Income",   items=income_items),
        CategoryGroup(label="Expenses", items=expense_items),
    ]


# ---------------------------------------------------------------------------
# Field updates
# ---------------------------------------------------------------------------

def update_transaction_field(tx_key: str, field: str, value: str) -> None:
    pred   = _FIELD_PREDICATES.get(field)
    if not pred:
        raise ValueError(f"Unknown field: {field}")
    tx_iri = mfl_iri_from_key(tx_key)  # Transactions live in MFL namespace

    store.update(f"""
        DELETE WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{tx_iri.value}> <{pred}> ?o .
            }}
        }}
    """)

    if not value.strip():
        return

    if field in ("category", "status"):
        store.update(f"""
            INSERT DATA {{
                GRAPH <{DATA_GRAPH.value}> {{
                    <{tx_iri.value}> <{pred}> <{value}> .
                }}
            }}
        """)
    else:
        esc = value.replace("\\", "\\\\").replace('"', '\\"')
        store.update(f"""
            INSERT DATA {{
                GRAPH <{DATA_GRAPH.value}> {{
                    <{tx_iri.value}> <{pred}> "{esc}"^^<http://www.w3.org/2001/XMLSchema#string> .
                }}
            }}
        """)


def bulk_update_transactions(tx_keys: list[str], field: str, value: str) -> int:
    if not value.strip():
        return 0
    for key in tx_keys:
        update_transaction_field(key, field, value)
    return len(tx_keys)


def create_manual_transaction(
    account_iri: NamedNode,
    date_str:    str,
    payee_raw:   str,
    amount:      Decimal,
    tx_type:     str,          # "debit" or "credit"
) -> NamedNode:
    """
    Create a single manually-entered transaction.
    Status defaults to Cleared for manual entries.
    Returns the new transaction IRI.
    """
    from app.core.ontology.iri_factory import new_transaction_iri

    tx_iri   = new_transaction_iri()
    type_iri = MFLX_TYPE_CREDIT if tx_type == "credit" else MFLX_TYPE_DEBIT
    esc      = payee_raw.replace("\\", "\\\\").replace('"', '\\"')

    store.update(f"""
        INSERT DATA {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{tx_iri.value}> a <{MFL_TRANSACTION.value}> ;
                    <{MFL_ON_ACCOUNT.value}>         <{account_iri.value}> ;
                    <{MFL}transactionDate>            "{date_str}"^^<http://www.w3.org/2001/XMLSchema#date> ;
                    <{MFL_AMOUNT.value}>              "{amount}"^^<http://www.w3.org/2001/XMLSchema#decimal> ;
                    <{MFL_TRANSACTION_TYPE.value}>    <{type_iri.value}> ;
                    <{MFL_TRANSACTION_STATUS.value}>  <{MFLX_STATUS_CLEARED.value}> ;
                    <{MFL_PAYEE_RAW.value}>           "{esc}"^^<http://www.w3.org/2001/XMLSchema#string> ;
                    <{MFL_IS_MANUAL_ENTRY.value}>     "true"^^<http://www.w3.org/2001/XMLSchema#boolean> .
            }}
        }}
    """)
    return tx_iri