"""Imports stop recreating curated categories (ADR-112).

When a user merges, deletes, or reparents a category, the *source path* an
importer carries (e.g. "Bills:Utilities:Cable") no longer matches the tree, so
the old find-or-create behaviour silently recreated the very category the user
just cleaned up. Two countermeasures, both pinned here:

  • a persistent source-path → my-category map, auto-recorded on merge / delete /
    reparent and consulted before any create; and
  • a match-only mode that routes anything still unmatched to "Needs Review"
    instead of forking the tree.

Deliberately **Qt-free** (Repository + ImportService import no Qt) so it runs on
the base interpreter:

    python3 tests/test_category_import_map.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.db.repository import Repository, UNCATEGORISED_ID
from mfl_desktop.import_engine.import_service import ImportService


def _fresh_repo() -> Repository:
    tmp = tempfile.mkdtemp(prefix="mfl_catmap_")
    return Repository(Path(tmp) / "f.mfl")


def _resolve(repo: Repository, raw: str) -> int:
    """Drive the real import-service resolution path for a raw source path."""
    return ImportService(repo)._resolve_category_id(raw)


# ── normalisation + path helpers ────────────────────────────────────────────

def test_normalize_folds_case_and_whitespace():
    assert Repository.normalize_category_path("Bills : Utilities") == "bills:utilities"
    assert Repository.normalize_category_path(["Bills", " Utilities "]) == "bills:utilities"
    assert Repository.normalize_category_path("  :  ") == ""


def test_path_string_round_trips_a_built_path():
    repo = _fresh_repo()
    leaf = repo.find_or_create_category_path(["Bills", "Cable and Internet"])
    assert repo._category_path_string(leaf) == "bills:cable and internet"
    assert repo.category_display_path(leaf) == "Bills : Cable and Internet"


# ── migration 0032 seeded the holding category ──────────────────────────────

def test_needs_review_category_exists():
    repo = _fresh_repo()
    rid = repo.needs_review_category_id()
    assert rid != UNCATEGORISED_ID
    row = repo.connection.execute(
        "SELECT name, source FROM category WHERE id = ?", (rid,),
    ).fetchone()
    assert row["name"] == "Needs Review"
    assert row["source"] == "system"


# ── find-only never creates ─────────────────────────────────────────────────

def test_find_category_path_is_create_free():
    repo = _fresh_repo()
    assert repo.find_category_path(["Made", "Up"]) is None
    leaf = repo.find_or_create_category_path(["Made", "Up"])
    assert repo.find_category_path(["Made", "Up"]) == leaf


# ── merge records a mapping that reroutes a re-import ────────────────────────

def test_merge_records_mapping_and_reimport_follows_it():
    repo = _fresh_repo()
    src = repo.find_or_create_category_path(["Bills", "Utilities", "Cable"])
    tgt = repo.find_or_create_category_path(["Bills", "Cable and Internet"])
    # The source leaf has no children (it's a leaf), so merge is allowed.
    repo.merge_categories([src], tgt)
    # The path the importer would carry now resolves to the merge target,
    # without recreating the old "Bills:Utilities:Cable" branch.
    assert repo.get_category_import_mapping("Bills:Utilities:Cable") == tgt
    assert _resolve(repo, "Bills:Utilities:Cable") == tgt
    assert repo.find_category_path(["Bills", "Utilities", "Cable"]) is None


# ── delete routes the path to Needs Review ──────────────────────────────────

def test_delete_records_mapping_to_needs_review():
    repo = _fresh_repo()
    cid = repo.find_or_create_category_path(["Fees", "Charges"])
    repo.delete_category(cid)
    review = repo.needs_review_category_id()
    assert repo.get_category_import_mapping("Fees:Charges") == review
    assert _resolve(repo, "Fees:Charges") == review


# ── reparent records the OLD path → same id ─────────────────────────────────

def test_reparent_records_old_path():
    repo = _fresh_repo()
    edu = repo.find_or_create_category_path(["Personal", "Education"])
    books = repo.find_or_create_category_path(["Personal", "Education", "Books"])
    # Move Books up to a top-level "Reading".
    reading = repo.find_or_create_category_path(["Reading"])
    repo.reparent_category(books, reading)
    # The old "Personal:Education:Books" path now reroutes to the moved leaf.
    assert repo.get_category_import_mapping("Personal:Education:Books") == books
    assert _resolve(repo, "Personal:Education:Books") == books


# ── match-only mode parks unknowns instead of creating ──────────────────────

def test_match_only_routes_unknown_to_needs_review():
    repo = _fresh_repo()
    assert repo.import_match_only_categories() is False  # default off
    repo.set_import_match_only_categories(True)
    review = repo.needs_review_category_id()
    # A brand-new path is parked, not created.
    assert _resolve(repo, "Some:Totally:New:Path") == review
    assert repo.find_category_path(["Some", "Totally", "New", "Path"]) is None
    # But an existing path still matches directly.
    leaf = repo.find_or_create_category_path(["Groceries"])
    assert _resolve(repo, "Groceries") == leaf


def test_match_only_off_still_creates():
    repo = _fresh_repo()
    assert _resolve(repo, "Fresh:Branch") != repo.needs_review_category_id()
    assert repo.find_category_path(["Fresh", "Branch"]) is not None


# ── an explicit mapping wins even over an existing same-named path ───────────

def test_mapping_overrides_existing_path():
    repo = _fresh_repo()
    decoy = repo.find_or_create_category_path(["Old", "Name"])
    canonical = repo.find_or_create_category_path(["New", "Name"])
    repo.set_category_import_mapping("Old:Name", canonical)
    assert _resolve(repo, "Old:Name") == canonical  # not the decoy
    # Listing exposes it for the management dialog.
    rows = repo.list_category_import_map()
    assert ("old:name", canonical, "New : Name") in rows
    repo.delete_category_import_mapping("Old:Name")
    assert _resolve(repo, "Old:Name") == decoy  # back to the existing path


# ── empty / blank source paths are Uncategorised ────────────────────────────

def test_blank_path_is_uncategorised():
    repo = _fresh_repo()
    assert _resolve(repo, "") == UNCATEGORISED_ID
    assert _resolve(repo, "   ") == UNCATEGORISED_ID


# ── bare-script runner ──────────────────────────────────────────────────────

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
