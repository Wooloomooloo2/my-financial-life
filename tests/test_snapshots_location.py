"""Configurable snapshot location + legacy back-compat (ADR-109).

Pins three things the relocation of the backups folder must not break:

- ``snapshot_dir`` honours the configured root (here a monkeypatched
  ``_snapshots_root``) and always names the folder ``MFL Snapshots``;
- ``existing_snapshots`` still surfaces an upgrader's history in the *legacy*
  ``Snapshots/`` folder beside the file (union read), so nothing is orphaned;
- ``prune`` thins across *both* folders.

Qt-free: we monkeypatch ``snapshots._snapshots_root`` so the ``app_session`` /
QSettings path (which needs PySide6) is never touched, letting this run on the
base interpreter (``python3 tests/test_snapshots_location.py``).
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import snapshots


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="mfl_snaploc_"))


def _point_root_at(root: Path):
    """Monkeypatch the configured snapshots root; returns a restore callable."""
    original = snapshots._snapshots_root
    snapshots._snapshots_root = lambda: root  # type: ignore[assignment]
    return lambda: setattr(snapshots, "_snapshots_root", original)


def test_snapshot_dir_uses_configured_root_and_mfl_name():
    root = _tmpdir()
    restore = _point_root_at(root)
    try:
        d = snapshots.snapshot_dir(_tmpdir() / "anywhere" / "Money.mfl")
        assert d == root / "MFL Snapshots"
        assert snapshots.SNAPSHOT_DIRNAME == "MFL Snapshots"
    finally:
        restore()


def test_existing_snapshots_unions_legacy_folder():
    """Backups in the old ``Snapshots/`` beside the file still show up."""
    db_dir = _tmpdir()
    db_path = db_dir / "Money.mfl"
    db_path.write_bytes(b"db")

    # Legacy folder beside the file (pre-ADR-109).
    legacy = db_dir / "Snapshots"
    legacy.mkdir()
    (legacy / "Money-20260101-120000.mfl").write_bytes(b"old")

    # New configured root elsewhere.
    root = _tmpdir()
    new_dir = root / "MFL Snapshots"
    new_dir.mkdir(parents=True)
    (new_dir / "Money-20260201-120000.mfl").write_bytes(b"new")

    restore = _point_root_at(root)
    try:
        snaps = snapshots.existing_snapshots(db_path)
        names = [p.name for p in snaps]
        assert "Money-20260101-120000.mfl" in names  # legacy surfaced
        assert "Money-20260201-120000.mfl" in names  # new surfaced
        # Sorted oldest→newest, so the newest is last (callers rely on [-1]).
        assert names[-1] == "Money-20260201-120000.mfl"
    finally:
        restore()


def test_prune_thins_across_both_folders():
    """A monthly-tier prune keeps one-per-month across legacy + new folders."""
    db_dir = _tmpdir()
    db_path = db_dir / "Money.mfl"
    db_path.write_bytes(b"db")
    legacy = db_dir / "Snapshots"
    legacy.mkdir()
    root = _tmpdir()
    new_dir = root / "MFL Snapshots"
    new_dir.mkdir(parents=True)

    # Two snapshots in the SAME old month, one in each folder → monthly tier
    # keeps only the newest of the month; the older one is deleted wherever it sits.
    old1 = legacy / "Money-20250101-090000.mfl"
    old2 = new_dir / "Money-20250115-090000.mfl"
    old1.write_bytes(b"a")
    old2.write_bytes(b"b")

    restore = _point_root_at(root)
    try:
        policy = snapshots.RetentionPolicy(
            subdaily_hours=24, daily_days=7, monthly_months=24,
        )
        now = datetime(2026, 6, 1, 12, 0, 0)  # both are well into the monthly tier
        deleted = snapshots.prune(db_path, policy, now)
        # Compare by name + on-disk effect (not Path identity) — on macOS the
        # resolved legacy dir is /private/var while the literal is /var.
        assert [p.name for p in deleted] == [old1.name]  # older-of-month dropped
        assert not old1.exists()        # …and it's the legacy-folder one
        assert old2.exists()            # newest of the month survives
    finally:
        restore()


def test_default_root_is_qt_appdata():
    """The real default (no monkeypatch) resolves via app_session → AppData.

    Skipped automatically when PySide6 isn't importable (base interpreter)."""
    try:
        from mfl_desktop import app_session  # noqa: F401
    except Exception:
        print("    (skipped: no PySide6 on this interpreter)")
        return
    root = snapshots._snapshots_root()
    assert isinstance(root, Path)


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
