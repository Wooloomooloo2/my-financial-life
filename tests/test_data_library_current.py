"""The pinned 'current file' row in the data library (ADR-109) + the app-data
relocation of the library folder (ADR-125).

The working file must be *visible* in Manage Data so the user always knows which
file they're editing — and it must never be loadable-onto-itself / renamable /
deletable. This pins the pure ``data_library.current_file`` half (the dialog
enforces non-deletability off the ``kind`` — covered by the offscreen smoke).

Also pins ADR-125: ``library_dir`` honours the configured app-data root and is
named ``MFL Library``, and ``list_saved`` unions the legacy beside-the-file
``Library/`` folder so an upgrader's saved datasets aren't orphaned.

Qt-free: ``data_library`` imports only ``snapshots`` (also Qt-free), and the
tests monkeypatch ``data_library._library_root`` so the ``app_session`` /
QSettings path (which needs PySide6) is never touched. Runs on the base
interpreter.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import data_library


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="mfl_dlc_"))


def _point_library_root_at(root: Path):
    """Monkeypatch the configured library root; returns a restore callable."""
    original = data_library._library_root
    data_library._library_root = lambda: root  # type: ignore[assignment]
    return lambda: setattr(data_library, "_library_root", original)


def test_current_file_row_shape():
    d = _tmpdir()
    db = d / "Money.mfl"
    db.write_bytes(b"hello world")
    row = data_library.current_file(db)
    assert row.kind == "current"
    assert row.name == "Money"
    assert row.path == db.resolve()
    assert row.size == 11


def test_current_file_distinct_from_saved_kind():
    """A current row is never confused with a real saved dataset."""
    db = _tmpdir() / "Money.mfl"
    db.write_bytes(b"x")
    restore = _point_library_root_at(_tmpdir())  # empty, isolated root
    try:
        assert data_library.current_file(db).kind != "saved"
        assert data_library.list_saved(db) == []  # nothing saved yet
    finally:
        restore()


def test_library_dir_uses_configured_root_and_mfl_name():
    root = _tmpdir()
    restore = _point_library_root_at(root)
    try:
        d = data_library.library_dir(_tmpdir() / "anywhere" / "Money.mfl")
        assert d == root / "MFL Library"
        assert data_library.LIBRARY_DIRNAME == "MFL Library"
    finally:
        restore()


def test_list_saved_reads_new_app_data_root():
    db = _tmpdir() / "Money.mfl"
    db.write_bytes(b"db")
    root = _tmpdir()
    new_dir = root / "MFL Library"
    new_dir.mkdir(parents=True)
    (new_dir / "baseline.mfl").write_bytes(b"saved")
    restore = _point_library_root_at(root)
    try:
        names = [f.name for f in data_library.list_saved(db)]
        assert names == ["baseline"]
    finally:
        restore()


def test_list_saved_unions_legacy_beside_file_folder():
    """Saved datasets in the old ``Library/`` beside the file still show up."""
    db_dir = _tmpdir()
    db = db_dir / "Money.mfl"
    db.write_bytes(b"db")
    # Legacy folder beside the file (pre-ADR-125).
    legacy = db_dir / "Library"
    legacy.mkdir()
    (legacy / "old_fixture.mfl").write_bytes(b"old")
    # New configured root elsewhere.
    root = _tmpdir()
    new_dir = root / "MFL Library"
    new_dir.mkdir(parents=True)
    (new_dir / "new_fixture.mfl").write_bytes(b"new")
    restore = _point_library_root_at(root)
    try:
        names = {f.name for f in data_library.list_saved(db)}
        assert names == {"old_fixture", "new_fixture"}  # both surfaced
    finally:
        restore()


def test_list_saved_dedupes_preferring_new_location():
    """A dataset present in both folders resolves to the new app-data copy."""
    db_dir = _tmpdir()
    db = db_dir / "Money.mfl"
    db.write_bytes(b"db")
    legacy = db_dir / "Library"
    legacy.mkdir()
    (legacy / "dup.mfl").write_bytes(b"legacy")
    root = _tmpdir()
    new_dir = root / "MFL Library"
    new_dir.mkdir(parents=True)
    (new_dir / "dup.mfl").write_bytes(b"new")
    restore = _point_library_root_at(root)
    try:
        saved = data_library.list_saved(db)
        assert len(saved) == 1
        assert saved[0].path == new_dir / "dup.mfl"   # new location wins
    finally:
        restore()


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
