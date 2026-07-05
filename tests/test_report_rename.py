"""Rename a saved report (ADR-141).

Previously the only way to change a saved report's name was to delete and
recreate it. The sidebar context menu now offers "Rename Report…", which goes
through the existing ``Repository.update_report(name=…)`` — non-empty, and
unique within a folder (top-level names may repeat, as SQLite treats a NULL
folder_id as distinct — same as create_report).

Qt-free.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop.db.repository import Repository


def _repo():
    db = Path(tempfile.mkdtemp(prefix="mfl_rep_")) / "m.mfl"
    return Repository(db)


def test_rename_updates_the_name():
    repo = _repo()
    r = repo.create_report(
        name="Bedford House ROI", type_key="income_expense",
        folder_id=None, filters_json="{}",
    )
    row = repo.update_report(r.id, name="Bedford House ROI (rental)")
    assert row.name == "Bedford House ROI (rental)"
    assert repo.get_report(r.id).name == "Bedford House ROI (rental)"


def test_rename_preserves_type_and_filters():
    repo = _repo()
    r = repo.create_report(
        name="A", type_key="income_expense", folder_id=None,
        filters_json='{"include_transfers":true}',
    )
    repo.update_report(r.id, name="B")
    got = repo.get_report(r.id)
    assert got.type == "income_expense"
    assert got.filters_json == '{"include_transfers":true}'


def test_rename_rejects_blank():
    repo = _repo()
    r = repo.create_report(
        name="A", type_key="income_expense", folder_id=None, filters_json="{}",
    )
    for bad in ("", "   "):
        try:
            repo.update_report(r.id, name=bad)
        except ValueError:
            continue
        raise AssertionError(f"blank name {bad!r} was accepted")


def test_rename_rejects_clash_within_a_folder():
    repo = _repo()
    fid = repo.create_report_folder("Property").id
    a = repo.create_report(name="ROI A", type_key="income_expense",
                           folder_id=fid, filters_json="{}")
    repo.create_report(name="ROI B", type_key="income_expense",
                       folder_id=fid, filters_json="{}")
    try:
        repo.update_report(a.id, name="ROI B")
    except ValueError:
        return
    raise AssertionError("a duplicate name within a folder was accepted")


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
