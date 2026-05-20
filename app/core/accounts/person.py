# ===========================================================================
# app/core/accounts/person.py
#
# Read and write the mrl:Person_1 instance.
#
# My Financial Life is a single-user application. The person is always
# stored as mrl:Person_1, consistent with MRL's integer IRI pattern
# (ADR-006). No IRI factory is needed for this specific entity.
#
# Also provides get_currencies() for populating the base currency dropdown.
# ===========================================================================

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from pyoxigraph import NamedNode, Literal

from app.data.store import store
from app.core.ontology.namespaces import (
    DATA_GRAPH,
    ONTOLOGY_GRAPH,
    MRL,
    RDF_TYPE,
    MRL_PERSON,
    MRL_FIRST_NAME,
    MRL_LAST_NAME,
    MRL_BASE_CURRENCY,
    MRL_CURRENCY,
    MRL_CURRENCY_CODE,
    MRL_CURRENCY_SYMBOL,
    XSD_STRING,
)

# The single Person IRI — consistent with MRL's Person_1 convention
PERSON_IRI = NamedNode(MRL + "Person_1")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Person:
    iri:               NamedNode
    first_name:        str
    last_name:         str
    base_currency_iri: Optional[NamedNode] = None
    base_currency_code: Optional[str] = None


@dataclass
class Currency:
    iri:    NamedNode
    code:   str
    symbol: str = ""

    @property
    def display(self) -> str:
        if self.symbol:
            return f"{self.code} ({self.symbol})"
        return self.code


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_person() -> Optional[Person]:
    """
    Return the Person_1 instance or None if no profile has been created.
    Uses quad pattern matching (ADR-007) for known-IRI property fetching.
    """
    # Check the person exists
    exists = any(
        True for _ in
        store.quads_for_pattern(PERSON_IRI, RDF_TYPE, MRL_PERSON, DATA_GRAPH)
    )
    if not exists:
        return None

    first_name = last_name = ""
    base_currency_iri = None
    base_currency_code = None

    for quad in store.quads_for_pattern(PERSON_IRI, None, None, DATA_GRAPH):
        pred = quad.predicate.value
        obj  = quad.object

        if pred == MRL_FIRST_NAME.value:
            first_name = obj.value
        elif pred == MRL_LAST_NAME.value:
            last_name = obj.value
        elif pred == MRL_BASE_CURRENCY.value:
            base_currency_iri = obj
            # Resolve currency code from ontology graph
            for cq in store.quads_for_pattern(obj, MRL_CURRENCY_CODE, None, ONTOLOGY_GRAPH):
                base_currency_code = cq.object.value

    return Person(
        iri=PERSON_IRI,
        first_name=first_name,
        last_name=last_name,
        base_currency_iri=base_currency_iri,
        base_currency_code=base_currency_code,
    )


def get_currencies() -> list[Currency]:
    """
    Return all Currency individuals from the ontology graph, sorted by code.
    Used to populate the base currency dropdown on the profile form.
    """
    currencies: dict[str, Currency] = {}

    for quad in store.quads_for_pattern(None, RDF_TYPE, MRL_CURRENCY, ONTOLOGY_GRAPH):
        currency_iri = quad.subject
        iri_str = currency_iri.value
        if iri_str not in currencies:
            currencies[iri_str] = Currency(iri=currency_iri, code="", symbol="")

    for iri_str, currency in currencies.items():
        iri = currency.iri
        for cq in store.quads_for_pattern(iri, MRL_CURRENCY_CODE, None, ONTOLOGY_GRAPH):
            currency.code = cq.object.value
        for cq in store.quads_for_pattern(iri, MRL_CURRENCY_SYMBOL, None, ONTOLOGY_GRAPH):
            currency.symbol = cq.object.value

    return sorted(
        [c for c in currencies.values() if c.code],
        key=lambda c: c.code
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def save_person(
    first_name: str,
    last_name: str,
    base_currency_iri: str,
) -> None:
    """
    Create or update the Person_1 instance.

    If Person_1 already exists, all existing triples are removed and
    replaced — this is a full overwrite, not a patch.

    Args:
        first_name:         Person's first name.
        last_name:          Person's last name.
        base_currency_iri:  Full IRI string for the chosen currency,
                            e.g. 'https://myretirementlife.app/ontology#Currency_GBP'
    """
    currency_node = NamedNode(base_currency_iri)

    # Remove any existing triples for Person_1 using SPARQL DELETE
    # (store.remove() does not accept None wildcards in pyoxigraph 0.5.x)
    store.update(f"""
        DELETE WHERE {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{PERSON_IRI.value}> ?p ?o .
            }}
        }}
    """)

    # Insert fresh triples using SPARQL UPDATE for correct XSD typing
    sparql = f"""
        PREFIX mrl:  <{MRL}>
        PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>

        INSERT DATA {{
            GRAPH <{DATA_GRAPH.value}> {{
                <{PERSON_IRI.value}> a mrl:Person ;
                    mrl:firstName        "{_escape(first_name)}"^^xsd:string ;
                    mrl:lastName         "{_escape(last_name)}"^^xsd:string ;
                    mrl:baseCurrency     <{currency_node.value}> .
            }}
        }}
    """
    store.update(sparql)


def _escape(value: str) -> str:
    """Escape double quotes and backslashes for safe SPARQL string insertion."""
    return value.replace("\\", "\\\\").replace('"', '\\"')