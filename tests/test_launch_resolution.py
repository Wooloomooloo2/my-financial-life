"""Launch database resolution (ADR-109).

The contract that fixes the "opened the wrong file" bug: with a main-file pointer
set, the resolver must make that file available or recover *explicitly* — it must
never silently fall through to a different file. Also pins the pointer-absent
branches (legacy dev file, first-run default) and ``--db`` handling.

Imports ``mfl_desktop.launch`` (→ ``app_session`` → PySide6), so run under an
interpreter that has PySide6 — e.g. the miniforge python3 — with no display
needed (no QApplication is constructed; all Qt-touching deps are monkeypatched):

    /opt/homebrew/Caskroom/miniforge/base/bin/python3 tests/test_launch_resolution.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import launch


def _tmpfile(name: str = "f.mfl") -> Path:
    d = Path(tempfile.mkdtemp(prefix="mfl_launch_"))
    p = d / name
    p.write_bytes(b"db")
    return p


def _args(db=None):
    return SimpleNamespace(db=db)


def _choice(**kw):
    base = dict(retry=False, open_other=False, new_file=False, path=None)
    base.update(kw)
    return SimpleNamespace(**base)


class _Dialog:
    """Fake recovery dialog: yields the queued choices, one per run()."""

    def __init__(self, choices):
        self._choices = list(choices)
        self.runs = 0

    def run(self):
        self.runs += 1
        return self._choices.pop(0)


class _Patch:
    """Save/restore a set of ``launch`` module attributes."""

    def __init__(self, **kw):
        self._kw = kw
        self._saved = {}

    def __enter__(self):
        for k, v in self._kw.items():
            self._saved[k] = getattr(launch, k)
            setattr(launch, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(launch, k, v)


def _fake_cloud(available_seq):
    """A stand-in for ``launch.cloud`` whose ensure_available pops a queue."""
    seq = list(available_seq)
    last = {"v": False}

    def ensure_available(path, *, timeout_s=10.0, poll_s=0.4, pump=None):
        last["v"] = seq.pop(0) if seq else False
        return last["v"]

    def is_available(path):
        return last["v"]

    return SimpleNamespace(ensure_available=ensure_available, is_available=is_available)


# ── tests ───────────────────────────────────────────────────────────────────

def test_pointer_available_opens():
    pointer = _tmpfile()
    with _Patch(
        last_db_path=lambda: pointer,
        begin_main_file_access=lambda: None,
        cloud=_fake_cloud([True]),
        _opens=lambda p: True,
    ):
        res = launch.resolve_database(_args(), dialog_factory=lambda p, r: None)
    assert res.db_path == pointer and res.exit_code is None
    assert res.seed_if_empty is False


def test_evicted_then_retry_materialises():
    pointer = _tmpfile()
    dlg = _Dialog([_choice(retry=True)])
    with _Patch(
        last_db_path=lambda: pointer,
        begin_main_file_access=lambda: None,
        cloud=_fake_cloud([False, True]),  # offline, then downloaded
        _opens=lambda p: True,
    ):
        res = launch.resolve_database(_args(), dialog_factory=lambda p, r: dlg)
    assert res.db_path == pointer
    assert dlg.runs == 1  # the recovery dialog was shown exactly once


def test_evicted_then_open_other():
    pointer = _tmpfile()
    other = _tmpfile("other.mfl")
    remembered = {"path": None}
    with _Patch(
        last_db_path=lambda: pointer,
        begin_main_file_access=lambda: None,
        cloud=_fake_cloud([False]),
        _opens=lambda p: True,
        remember_last_db=lambda p: remembered.__setitem__("path", Path(p)),
    ):
        dlg = _Dialog([_choice(open_other=True, path=other)])
        res = launch.resolve_database(_args(), dialog_factory=lambda p, r: dlg)
    assert res.db_path == other
    assert remembered["path"] == other  # pointer repointed to the chosen file


def test_evicted_then_new_file_seeds():
    pointer = _tmpfile()
    fresh = Path(tempfile.mkdtemp(prefix="mfl_launch_")) / "new.mfl"
    with _Patch(
        last_db_path=lambda: pointer,
        begin_main_file_access=lambda: None,
        cloud=_fake_cloud([False]),
        _opens=lambda p: True,
        remember_last_db=lambda p: None,
    ):
        dlg = _Dialog([_choice(new_file=True, path=fresh)])
        res = launch.resolve_database(_args(), dialog_factory=lambda p, r: dlg)
    assert res.db_path == fresh and res.seed_if_empty is True
    # A recovery-picked new file is NOT the unattended first-run default, so it
    # must not trigger the sandbox folder picker (ADR-125).
    assert res.first_run_default is False


def test_dialog_closed_quits():
    pointer = _tmpfile()
    with _Patch(
        last_db_path=lambda: pointer,
        begin_main_file_access=lambda: None,
        cloud=_fake_cloud([False]),
        _opens=lambda p: True,
        remember_last_db=lambda p: None,
    ):
        dlg = _Dialog([_choice()])  # all-False == closed the dialog
        res = launch.resolve_database(_args(), dialog_factory=lambda p, r: dlg)
    assert res.exit_code == 0 and res.db_path is None


class _FakeLocation:
    """Stand-in for sandbox.ResolvedLocation: records start/stop and a path."""

    def __init__(self, path):
        self.path = Path(path)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.stopped = True


def test_sandbox_bookmark_carries_location():
    """ADR-125: when a security-scoped bookmark resolves, the resolver opens the
    bookmark's path (authoritative over a stale plain pointer) and carries the
    live location out in the Resolution so the caller keeps access for the
    session. (Starting access is ``begin_main_file_access``'s job — see the
    app_session test — so it's bypassed here along with the rest of that hook.)"""
    bookmarked = _tmpfile("from_bookmark.mfl")
    loc = _FakeLocation(bookmarked)
    with _Patch(
        # The plain pointer is a now-stale path; the bookmark is authoritative.
        last_db_path=lambda: Path("/stale/old/path.mfl"),
        begin_main_file_access=lambda: loc,
        cloud=_fake_cloud([True]),
        _opens=lambda p: True,
    ):
        res = launch.resolve_database(_args(), dialog_factory=lambda p, r: None)
    assert res.db_path == bookmarked          # bookmark path wins over stale path
    assert res.location is loc                  # carried for the session


def test_sandbox_bookmark_released_on_recovery_swap():
    """If the bookmarked file can't be opened and the user picks another, the
    bookmark's security-scoped access is released."""
    other = _tmpfile("other.mfl")
    loc = _FakeLocation(_tmpfile("gone.mfl"))
    with _Patch(
        last_db_path=lambda: Path("/stale/old/path.mfl"),
        begin_main_file_access=lambda: loc,
        cloud=_fake_cloud([False]),
        _opens=lambda p: True,
        remember_last_db=lambda p: None,
    ):
        dlg = _Dialog([_choice(open_other=True, path=other)])
        res = launch.resolve_database(_args(), dialog_factory=lambda p, r: dlg)
    assert res.db_path == other
    assert loc.stopped is True


def test_no_pointer_uses_first_run_default():
    fresh = Path(tempfile.mkdtemp(prefix="mfl_launch_")) / "My Financial Life" / "MyFinancialLife.mfl"
    with _Patch(
        last_db_path=lambda: None,
        LEGACY_DB_CANDIDATES=[],  # no dev file in scope
        first_run_default_path=lambda: fresh,
    ):
        res = launch.resolve_database(_args(), dialog_factory=lambda p, r: None)
    assert res.db_path == fresh and res.seed_if_empty is True
    # Flagged so the caller can offer the sandbox first-run folder picker (ADR-125).
    assert res.first_run_default is True


def test_legacy_used_only_when_pointer_absent():
    legacy = _tmpfile("mfl_dev.mfl")
    with _Patch(
        last_db_path=lambda: None,
        LEGACY_DB_CANDIDATES=[legacy],
    ):
        res = launch.resolve_database(_args(), dialog_factory=lambda p, r: None)
    assert res.db_path == legacy and res.seed_if_empty is False


def test_sandboxed_skips_legacy_cwd_uses_first_run():
    """ADR-125: under the sandbox the cwd dev file (mfl_dev.mfl) is unreadable, so
    a first launch with no pointer goes to the first-run default, not the legacy
    cwd file — even when one exists."""
    legacy = _tmpfile("mfl_dev.mfl")
    fresh = Path(tempfile.mkdtemp(prefix="mfl_launch_")) / "MyFinancialLife.mfl"
    with _Patch(
        last_db_path=lambda: None,
        sandbox=SimpleNamespace(is_sandboxed=lambda: True),
        LEGACY_DB_CANDIDATES=[legacy],
        first_run_default_path=lambda: fresh,
    ):
        res = launch.resolve_database(_args(), dialog_factory=lambda p, r: None)
    assert res.db_path == fresh and res.seed_if_empty is True


def test_explicit_db_present_used():
    f = _tmpfile()
    res = launch.resolve_database(_args(db=f), dialog_factory=lambda p, r: None)
    assert res.db_path == f


def test_explicit_db_missing_exits_1():
    missing = Path(tempfile.mkdtemp(prefix="mfl_launch_")) / "nope.mfl"
    res = launch.resolve_database(_args(db=missing), dialog_factory=lambda p, r: None)
    assert res.exit_code == 1 and res.db_path is None


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
