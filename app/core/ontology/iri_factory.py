# ===========================================================================
# app/core/ontology/iri_factory.py
#
# IRI generation for user-created instances.
#
# Two strategies (ADR-006):
#   - Accounts and Person: mrl:ClassName_N  (integer, matches MRL pattern)
#   - Transactions, Valuations, etc: mfl:ClassName_<uuid6>  (collision-safe)
# ===========================================================================

import uuid

from pyoxigraph import NamedNode

from app.data.store import store
from app.core.ontology.namespaces import DATA_GRAPH, MRL, MFL, RDF_TYPE


def next_account_iri(account_class: NamedNode) -> NamedNode:
    """
    Return the next available integer IRI for an account class.
    Scans all existing instances to find the highest N, returns N+1.

    Example: if mrl:CashAccount_1 and mrl:CashAccount_2 exist, returns
             mrl:CashAccount_3
    """
    class_local = account_class.value.split("#")[1]  # e.g. "CashAccount"
    prefix = MRL + class_local + "_"
    max_n = 0

    for quad in store.quads_for_pattern(None, RDF_TYPE, account_class, DATA_GRAPH):
        iri = quad.subject.value
        if iri.startswith(prefix):
            try:
                n = int(iri[len(prefix):])
                max_n = max(max_n, n)
            except ValueError:
                pass

    return NamedNode(prefix + str(max_n + 1))


def new_transaction_iri() -> NamedNode:
    """Return a new unique IRI for a Transaction instance."""
    return NamedNode(MFL + "Transaction_" + uuid.uuid4().hex[:8])


def new_valuation_iri() -> NamedNode:
    """Return a new unique IRI for a ValuationEvent instance."""
    return NamedNode(MFL + "ValuationEvent_" + uuid.uuid4().hex[:8])


def new_import_batch_iri() -> NamedNode:
    """Return a new unique IRI for an ImportBatch instance."""
    return NamedNode(MFL + "ImportBatch_" + uuid.uuid4().hex[:8])


def iri_key(iri: NamedNode) -> str:
    """
    Return the local name of an IRI for use in URLs.
    e.g. 'https://myretirementlife.app/ontology#CashAccount_1' → 'CashAccount_1'
    """
    return iri.value.split("#")[-1]


def iri_from_key(key: str) -> NamedNode:
    """
    Reconstruct a full MRL IRI from a URL key.
    e.g. 'CashAccount_1' → NamedNode('https://myretirementlife.app/ontology#CashAccount_1')
    Use for accounts and persons only.
    """
    return NamedNode(MRL + key)


def mfl_iri_from_key(key: str) -> NamedNode:
    """
    Reconstruct a full MFL IRI from a URL key.
    e.g. 'Transaction_abc12345' → NamedNode('https://myfinanciallife.app/ontology#Transaction_abc12345')
    Use for transactions, valuation events, payees, category rules, import batches.
    """
    return NamedNode(MFL + key)
