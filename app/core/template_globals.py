# ===========================================================================
# app/core/template_globals.py
#
# Jinja2 template global functions injected into every template.
# Mirrors the pattern used in My Retirement Life.
#
# Two globals are registered:
#   user_initials()  — returns the person's initials for the avatar
#   setup_state()    — returns setup progress for the onboarding banner
#
# Registered in main.py via:
#   templates.env.globals["user_initials"] = user_initials
#   templates.env.globals["setup_state"]   = setup_state
# ===========================================================================

from dataclasses import dataclass

from app.data.store import store
from app.core.ontology.namespaces import (
    DATA_GRAPH,
    MRL_PERSON,
    MRL_FIRST_NAME,
    MRL_LAST_NAME,
    MFL_APP_SETTINGS,
    RDF_TYPE,
    MRL_CASH_ACCOUNT,
    MRL_INVESTMENT_ACCOUNT,
    MRL_CREDIT_CARD,
    MRL_PROPERTY_ASSET,
    MFL_TRANSACTION,
    MFL_CATEGORY_RULE,
)
from pyoxigraph import NamedNode


# ---------------------------------------------------------------------------
# Setup steps for My Financial Life (4 steps)
#
# Step 1 — Profile created     (mrl:Person_1 exists with name + base currency)
# Step 2 — First account added (any account subclass instance exists)
# Step 3 — First transaction   (any mfl:Transaction instance exists)
# Step 4 — First category rule (any mfl:CategoryRule instance exists)
# ---------------------------------------------------------------------------

@dataclass
class SetupState:
    setup_all_done:   bool
    setup_steps_done: int
    setup_next_url:   str
    setup_next_label: str


def _any_exists(rdf_class: NamedNode) -> bool:
    """Return True if at least one instance of rdf_class exists in the data graph."""
    for _ in store.quads_for_pattern(None, RDF_TYPE, rdf_class, DATA_GRAPH):
        return True
    return False


def _profile_exists() -> bool:
    """Return True if a Person instance exists with a first name set."""
    for quad in store.quads_for_pattern(None, RDF_TYPE, MRL_PERSON, DATA_GRAPH):
        person_iri = quad.subject
        for _ in store.quads_for_pattern(person_iri, MRL_FIRST_NAME, None, DATA_GRAPH):
            return True
    return False


def _account_exists() -> bool:
    """Return True if any account of any type exists in the data graph."""
    for cls in [MRL_CASH_ACCOUNT, MRL_INVESTMENT_ACCOUNT, MRL_CREDIT_CARD, MRL_PROPERTY_ASSET]:
        if _any_exists(cls):
            return True
    return False


def setup_state() -> SetupState:
    """
    Calculate the current setup progress and return the next action.
    Called on every page render via Jinja2 template global.
    """
    step1 = _profile_exists()
    step2 = step1 and _account_exists()
    step3 = step2 and _any_exists(MFL_TRANSACTION)
    step4 = step3 and _any_exists(MFL_CATEGORY_RULE)

    steps_done = sum([step1, step2, step3, step4])
    all_done   = steps_done == 4

    if not step1:
        next_url   = "/settings/profile"
        next_label = "Create your profile"
    elif not step2:
        next_url   = "/accounts/new"
        next_label = "Add your first account"
    elif not step3:
        next_url   = "/import"
        next_label = "Import or add a transaction"
    else:
        next_url   = "/categories"
        next_label = "Set up category rules"

    return SetupState(
        setup_all_done=all_done,
        setup_steps_done=steps_done,
        setup_next_url=next_url,
        setup_next_label=next_label,
    )


def user_initials() -> str:
    """
    Return the person's initials for the avatar circle in the header.
    Falls back to '?' if no profile has been created yet.
    """
    for quad in store.quads_for_pattern(None, RDF_TYPE, MRL_PERSON, DATA_GRAPH):
        person_iri = quad.subject
        first = last = ""
        for q in store.quads_for_pattern(person_iri, MRL_FIRST_NAME, None, DATA_GRAPH):
            first = str(q.object.value) if hasattr(q.object, "value") else ""
        for q in store.quads_for_pattern(person_iri, MRL_LAST_NAME, None, DATA_GRAPH):
            last = str(q.object.value) if hasattr(q.object, "value") else ""
        initials = (first[:1] + last[:1]).upper()
        return initials if initials else "?"
    return "?"