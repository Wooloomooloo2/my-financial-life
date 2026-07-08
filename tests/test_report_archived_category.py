"""Reports resolve archived categories that still have transactions (ADR-143).

A category can be archived while historical transactions still reference it
(archiving doesn't reassign them). The report windows built their category
name / rollup maps from the *live-only* tree, so such a transaction showed as a
bare "id=N" row (owner hit this: an archived "Legal and Closing Costs" appeared
as "id=168"). The maps now include archived categories, while the filter picker
stays live-only.

Qt-free — validates the repo + rollup-helper layer the windows wire up.
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.db.repository import Repository
from mfl_desktop.reports import category_group_map, category_root_map


def _repo():
    repo = Repository(Path(tempfile.mkdtemp(prefix="mfl_arch_")) / "m.mfl")
    acct = repo.create_account(name="Cur", type_key="cash", currency="GBP",
                               opening_balance=Decimal("0"))
    parent = repo.create_category("Beacon Home", None, "expense")
    child = repo.create_category("Legal and Closing Costs", parent, "expense")
    repo.insert_transaction(
        account_id=acct.id, posted_date="2024-05-01", amount=Decimal("-2206.40"),
        payee_id=None, category_id=child, status="cleared", memo="",
        import_hash=None, import_batch_id=None,
    )
    # Archive the child but leave the transaction referencing it — the real
    # post-import state (archiving doesn't reassign historical rows).
    repo.connection.execute(
        "UPDATE category SET archived_at = datetime('now') WHERE id = ?",
        (child,),
    )
    repo.commit()
    return repo, parent, child


def test_archived_category_excluded_live_but_available_when_asked():
    repo, parent, child = _repo()
    assert child not in {c.id for c in repo.list_category_tree()}
    assert child in {c.id for c in repo.list_category_tree(include_archived=True)}


def test_display_maps_resolve_archived_name_and_rollup():
    repo, parent, child = _repo()

    # The bug: the live-only tree (what the filter picker uses) can't name it.
    live_by_id = {c.id: c for c in repo.list_category_tree()}
    assert child not in live_by_id                    # → would render "id=168"

    # The fix: the archived-inclusive tree names it and rolls it up to a real,
    # named bucket (never an "id=N" placeholder) at both group and top level.
    cats = repo.list_category_tree(include_archived=True)
    by_id = {c.id: c for c in cats}
    assert by_id[child].name == "Legal and Closing Costs"

    gmap = category_group_map(cats)
    rmap = category_root_map(cats)
    for level, m in (("group", gmap), ("top", rmap)):
        bucket = m.get(child, child)
        node = by_id.get(bucket)
        assert node is not None, f"{level} rollup left {child} unresolved"
        assert not str(node.name).startswith("id="), level
    # Top-level rollup lands on the (live) parent root.
    assert rmap.get(child) == parent


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
