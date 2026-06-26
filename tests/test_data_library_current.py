"""The pinned 'current file' row in the data library (ADR-109).

The working file must be *visible* in Manage Data so the user always knows which
file they're editing — and it must never be loadable-onto-itself / renamable /
deletable. This pins the pure ``data_library.current_file`` half (the dialog
enforces non-deletability off the ``kind`` — covered by the offscreen smoke).

Qt-free: ``data_library`` imports only ``snapshots`` (also Qt-free). Runs on the
base interpreter.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import data_library


def test_current_file_row_shape():
    d = Path(tempfile.mkdtemp(prefix="mfl_dlc_"))
    db = d / "Money.mfl"
    db.write_bytes(b"hello world")
    row = data_library.current_file(db)
    assert row.kind == "current"
    assert row.name == "Money"
    assert row.path == db.resolve()
    assert row.size == 11


def test_current_file_distinct_from_saved_kind():
    """A current row is never confused with a real saved dataset."""
    d = Path(tempfile.mkdtemp(prefix="mfl_dlc_"))
    db = d / "Money.mfl"
    db.write_bytes(b"x")
    assert data_library.current_file(db).kind != "saved"
    assert data_library.list_saved(db) == []  # no Library/ folder yet


# ── bare-script runner ──────────────────────────────────────────────────────

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
