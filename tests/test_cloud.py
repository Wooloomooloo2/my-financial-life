"""Cloud-file availability helpers (ADR-109).

Pins the provider-agnostic ``mfl_desktop.cloud`` logic the launch resolver leans
on to tell "downloading / offline" apart from "really gone", so it never falls
back to a different file behind the user's back.

Qt-free — runs on the base interpreter (``python3 tests/test_cloud.py``) or under
pytest. ``request_download`` is exercised only against a missing ``brctl`` /
absent file so it stays hermetic and never touches a real cloud provider.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mfl_desktop import cloud


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="mfl_cloud_"))


def test_is_available_true_for_real_file():
    p = _tmpdir() / "live.mfl"
    p.write_bytes(b"hello")
    assert cloud.is_available(p) is True


def test_is_available_false_for_missing_and_empty():
    d = _tmpdir()
    assert cloud.is_available(d / "missing.mfl") is False
    empty = d / "empty.mfl"
    empty.touch()  # zero bytes == not-yet-materialised placeholder
    assert cloud.is_available(empty) is False


def test_icloud_placeholder_detection():
    d = _tmpdir()
    real = d / "Money.mfl"
    assert cloud.icloud_placeholder(real) is None
    placeholder = d / ".Money.mfl.icloud"
    placeholder.write_bytes(b"")
    found = cloud.icloud_placeholder(real)
    assert found is not None and found.name == ".Money.mfl.icloud"


def test_ensure_available_immediate():
    p = _tmpdir() / "ready.mfl"
    p.write_bytes(b"x")
    assert cloud.ensure_available(p, timeout_s=1.0) is True


def test_ensure_available_appears_mid_poll():
    """A file that materialises partway through the wait is picked up."""
    p = _tmpdir() / "late.mfl"

    def _materialise() -> None:
        time.sleep(0.3)
        p.write_bytes(b"now here")

    threading.Thread(target=_materialise, daemon=True).start()
    assert cloud.ensure_available(p, timeout_s=3.0, poll_s=0.1) is True


def test_ensure_available_times_out():
    p = _tmpdir() / "never.mfl"
    start = time.monotonic()
    assert cloud.ensure_available(p, timeout_s=0.5, poll_s=0.1) is False
    # Returned roughly within the budget, not hung.
    assert time.monotonic() - start < 3.0


def test_request_download_swallows_failures():
    # Missing file + (likely) missing brctl: must never raise.
    cloud.request_download(_tmpdir() / "ghost.mfl")


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
