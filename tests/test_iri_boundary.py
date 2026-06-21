"""Guard the MFL ↔ MRL IRI boundary (M1, ADR-005 / ADR-006).

Accounts and the Person carry **``mrl:``-namespaced** IRIs — that prefix
is the *join key* My Retirement Life matches MFL's entities on when it
imports the file-based RDF export (workstream M). Everything MFL owns
privately (transactions, transfers, schedules, budgets, reports, …) uses
the **``mfl:``** namespace and is invisible to MRL.

The 1.0 backlog (M1) calls for a guard "so a future refactor can't
silently change the prefix." This file is that guard. If someone renames
``mrl:`` → ``mfl:`` in the account minter, flips the seed person/account
IRIs, or lets a private entity leak into the ``mrl:`` space, one of these
assertions fails loudly.

Deliberately **Qt-free** so it runs on the base interpreter
(``python3 tests/test_iri_boundary.py``) as well as under pytest — the
seed-site check reads source as text rather than importing the Qt-bound
``__main__`` / ``cli`` modules.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

# Make the repo root importable when run as a bare script from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.account_types import ACCOUNT_TYPES
from mfl_desktop.db.repository import (
    Repository,
    new_budget_iri,
    new_goal_iri,
    new_import_batch_iri,
    new_report_folder_iri,
    new_report_iri,
    new_rule_iri,
    new_scheduled_txn_iri,
    new_security_iri,
    new_statement_iri,
    new_transaction_iri,
    new_transfer_iri,
)

# The two namespaces. MRL = the shared cross-app identity space (the join
# key); MFL = MFL-private entities MRL never sees.
MRL_PREFIX = "mrl:"
MFL_PREFIX = "mfl:"

# The exact seed IRIs minted into every brand-new file (the very first
# Person + cash account). MRL matches the user's identity on Person_1, so
# these strings are a contract, not an implementation detail.
SEED_PERSON_IRI = "mrl:Person_1"
SEED_ACCOUNT_IRI = "mrl:CashAccount_1"

# An IRI minted by ``_next_account_iri`` looks like ``mrl:CashAccount_3``.
_ACCOUNT_IRI_RE = re.compile(r"^mrl:([A-Za-z]+)_(\d+)$")


def _fresh_repo() -> Repository:
    """A migrated, empty Repository on a throwaway file."""
    tmp = tempfile.mkdtemp(prefix="mfl_iri_guard_")
    return Repository(Path(tmp) / "guard.mfl")


def test_account_minter_uses_mrl_namespace():
    """Every account family mints an ``mrl:<ClassName>_<n>`` IRI — never
    ``mfl:``. The class segment must equal the type's declared
    ``class_name`` so the MRL subclass mapping stays correct."""
    repo = _fresh_repo()
    try:
        for spec in ACCOUNT_TYPES:
            acct = repo.create_account(
                name=f"Guard {spec.key}", type_key=spec.key, currency="GBP",
            )
            iri = acct.iri
            assert iri.startswith(MRL_PREFIX), (
                f"{spec.key!r} account IRI {iri!r} lost the {MRL_PREFIX!r} "
                f"prefix — MRL would no longer match it."
            )
            assert not iri.startswith(MFL_PREFIX), (
                f"{spec.key!r} account IRI {iri!r} slipped into the private "
                f"{MFL_PREFIX!r} namespace."
            )
            m = _ACCOUNT_IRI_RE.match(iri)
            assert m is not None, f"account IRI {iri!r} is not mrl:<Class>_<n>"
            assert m.group(1) == spec.class_name, (
                f"{spec.key!r} minted class {m.group(1)!r}, expected "
                f"{spec.class_name!r}"
            )
    finally:
        repo.close()


def test_account_numbering_is_sequential_and_class_scoped():
    """Suffixes increment within a class and each class numbers
    independently (so deleting/adding in one family never shifts
    another's IRIs)."""
    repo = _fresh_repo()
    try:
        c1 = repo.create_account(name="Cash 1", type_key="cash", currency="GBP")
        c2 = repo.create_account(name="Cash 2", type_key="cash", currency="GBP")
        cc = repo.create_account(name="Card", type_key="credit", currency="GBP")
        assert c1.iri == "mrl:CashAccount_1"
        assert c2.iri == "mrl:CashAccount_2"
        # Credit numbering is independent of how many cash accounts exist.
        assert cc.iri == "mrl:CreditCardAccount_1"
    finally:
        repo.close()


def test_next_account_iri_prefix_is_mrl():
    """Direct unit check on the private minter — the prefix is exactly
    ``mrl:<class>_`` regardless of which class is asked for."""
    repo = _fresh_repo()
    try:
        # No accounts yet → first of any class is _1, in the mrl: space.
        assert repo._next_account_iri("CashAccount") == "mrl:CashAccount_1"
        assert repo._next_account_iri("PensionAccount") == "mrl:PensionAccount_1"
        for iri in (
            repo._next_account_iri("CashAccount"),
            repo._next_account_iri("InvestmentAccount"),
        ):
            assert iri.startswith(MRL_PREFIX) and not iri.startswith(MFL_PREFIX)
    finally:
        repo.close()


def test_account_is_queryable_by_its_mrl_iri():
    """The IRI is a usable handle — ``get_account_by_iri`` round-trips it.
    This is exactly the lookup MRL performs after reading the export."""
    repo = _fresh_repo()
    try:
        acct = repo.create_account(name="RT", type_key="savings", currency="GBP")
        fetched = repo.get_account_by_iri(acct.iri)
        assert fetched is not None and fetched.id == acct.id
    finally:
        repo.close()


def test_private_entities_use_mfl_namespace():
    """The MFL-private entities must stay in the ``mfl:`` space — they are
    NOT part of the MRL contract and must never collide with it."""
    minters = {
        "transaction": new_transaction_iri,
        "import_batch": new_import_batch_iri,
        "transfer": new_transfer_iri,
        "scheduled_txn": new_scheduled_txn_iri,
        "budget": new_budget_iri,
        "goal": new_goal_iri,
        "report": new_report_iri,
        "report_folder": new_report_folder_iri,
        "statement": new_statement_iri,
        "security": new_security_iri,
        "rule": new_rule_iri,
    }
    for name, fn in minters.items():
        iri = fn()
        assert iri.startswith(MFL_PREFIX), (
            f"{name} IRI {iri!r} is not in the private {MFL_PREFIX!r} namespace"
        )
        assert not iri.startswith(MRL_PREFIX), (
            f"{name} IRI {iri!r} leaked into the MRL join-key namespace"
        )


def test_seed_iris_are_pinned_at_both_seed_sites():
    """The first-file seed mints ``mrl:Person_1`` + ``mrl:CashAccount_1``
    in two places (GUI launch ``__main__._seed_starter_db`` and
    ``cli.cmd_init``). Both must stay on the MRL prefix and in sync.

    Checked at the source level (text) so this test needs no Qt — the
    GUI ``__main__`` module imports PySide6 at load time."""
    for rel in ("mfl_desktop/__main__.py", "mfl_desktop/cli.py"):
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        assert SEED_PERSON_IRI in src, (
            f"{rel} no longer seeds {SEED_PERSON_IRI!r} — MRL identity match "
            f"would break."
        )
        assert SEED_ACCOUNT_IRI in src, (
            f"{rel} no longer seeds {SEED_ACCOUNT_IRI!r}."
        )
        # Defensive: the seed must not have been flipped to the private space.
        assert "mfl:Person" not in src, f"{rel} seeds a person in the mfl: space"
        assert "mfl:CashAccount" not in src, (
            f"{rel} seeds the starter account in the mfl: space"
        )


# ── bare-script runner (no pytest required) ────────────────────────────────

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
        except Exception as e:  # noqa: BLE001 — surface setup errors too
            failures += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    total = len(tests)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
