"""macOS App Sandbox foundation — detection + security-scoped bookmarks (ADR-125).

Pins ``mfl_desktop.sandbox`` (increment A of the ADR-125 arc): the sandbox
detection, the bookmark primitives, and the encode/decode policy the launch
resolver will lean on so a sandboxed Mac App Store build can re-open the user's
``.mfl`` across launches — while degrading cleanly to plain paths in dev.

Qt-free — runs on the base interpreter (``python3 tests/test_sandbox.py``) or
under pytest. The tests run **unsandboxed** (no ``APP_SANDBOX_CONTAINER_ID``), so
they pin both the universal behaviour and the no-Mac/no-PyObjC fallbacks. The
real native bookmark round-trip is exercised only when PyObjC's Foundation is
importable (it is on a dev Mac); elsewhere that one test self-skips so the file
stays green on Linux/Windows CI.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import sandbox


def _tmpfile() -> Path:
    p = Path(tempfile.mkdtemp(prefix="mfl_sandbox_")) / "money.mfl"
    p.write_bytes(b"hi")
    return p


# ── detection ────────────────────────────────────────────────────────────────


def test_is_sandboxed_false_without_env():
    # The test process is never sandboxed; the container env var is absent.
    assert "APP_SANDBOX_CONTAINER_ID" not in os.environ
    assert sandbox.is_sandboxed() is False


def test_is_sandboxed_requires_macos(monkeypatch=None):
    # Even with the env var set, non-macOS is never sandboxed.
    old = os.environ.get("APP_SANDBOX_CONTAINER_ID")
    os.environ["APP_SANDBOX_CONTAINER_ID"] = "abc.MyFinancialLife"
    try:
        expected = sys.platform == "darwin"
        assert sandbox.is_sandboxed() is expected
    finally:
        if old is None:
            os.environ.pop("APP_SANDBOX_CONTAINER_ID", None)
        else:
            os.environ["APP_SANDBOX_CONTAINER_ID"] = old


def test_bookmarks_supported_matches_platform():
    if sys.platform != "darwin":
        assert sandbox.bookmarks_supported() is False
    # On macOS it depends on PyObjC being installed; just assert it's a bool and
    # consistent with whether Foundation imports.
    assert isinstance(sandbox.bookmarks_supported(), bool)


# ── policy: encode / decode ──────────────────────────────────────────────────


def test_encode_unsandboxed_is_tagged_plain_path():
    p = _tmpfile()
    token = sandbox.encode_location(p)
    assert token.startswith("path:")
    # Resolves the absolute path back, no scope handle, not stale.
    loc = sandbox.decode_location(token)
    assert loc is not None
    assert loc.path == p.resolve()
    assert loc.stale is False
    # start/stop are no-ops on a plain location and the context manager yields it.
    with loc as resolved:
        assert resolved == p.resolve()


def test_decode_accepts_bare_legacy_path():
    # The pre-sandbox session/last_db_path stored a bare absolute path.
    p = _tmpfile()
    loc = sandbox.decode_location(str(p))
    assert loc is not None
    assert loc.path == p
    assert loc.stale is False


def test_decode_empty_token_is_none():
    assert sandbox.decode_location("") is None
    assert sandbox.decode_location(None) is None  # type: ignore[arg-type]


def test_decode_garbage_bookmark_is_none():
    # A bm:-tagged blob that isn't valid base64 bookmark data resolves to None
    # (when supported) — the caller treats that as "no remembered file".
    result = sandbox.decode_location("bm:not-a-real-bookmark!!")
    assert result is None


def test_plain_location_start_stop_safe():
    loc = sandbox.ResolvedLocation(path=Path("/tmp/x.mfl"), stale=False, _url=None)
    assert loc.start() is True       # nothing to scope → "held"
    loc.stop()                       # idempotent no-op
    loc.stop()


# ── native primitive (macOS + PyObjC only) ───────────────────────────────────


def test_security_scoped_bookmark_round_trip():
    if not sandbox.bookmarks_supported():
        print("    (skipped — PyObjC/Foundation not available)")
        return
    p = _tmpfile()
    blob = sandbox.create_security_scoped_bookmark(p)
    assert blob, "expected a base64 bookmark string on a PyObjC-capable Mac"
    loc = sandbox.resolve_security_scoped_bookmark(blob)
    assert loc is not None
    # Resolved to the same file (allowing /private symlink normalisation).
    assert loc.path.name == p.name
    assert loc.path.resolve() == p.resolve()
    # Scoped access brackets cleanly.
    loc.start()
    loc.stop()


def test_encode_round_trip_via_bookmark_when_supported(monkeypatch=None):
    # Force the sandboxed policy branch (is_sandboxed → True) on a PyObjC-capable
    # Mac and confirm encode produces a bookmark that decode can resolve.
    if not sandbox.bookmarks_supported():
        print("    (skipped — PyObjC/Foundation not available)")
        return
    p = _tmpfile()
    orig = sandbox.is_sandboxed
    sandbox.is_sandboxed = lambda: True  # type: ignore[assignment]
    try:
        token = sandbox.encode_location(p)
        assert token.startswith("bm:")
        loc = sandbox.decode_location(token)
        assert loc is not None
        assert loc.path.resolve() == p.resolve()
    finally:
        sandbox.is_sandboxed = orig  # type: ignore[assignment]


# ── bare-script runner ───────────────────────────────────────────────────────


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
