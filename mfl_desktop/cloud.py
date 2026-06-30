"""Provider-agnostic cloud-file availability (ADR-109).

A user's ``.mfl`` increasingly lives in a cloud-synced folder — iCloud Drive
(which backs macOS Documents/Desktop by default), OneDrive (often on by default
on Windows), Dropbox, Google Drive. Those providers *evict* a file that hasn't
been used recently: its bytes leave the local disk and only a lightweight
placeholder remains, so a plain :py:meth:`pathlib.Path.exists` can read False —
or an ``open`` can block for seconds while the bytes are fetched — even though
the file genuinely exists in the user's account.

The launch resolver must not treat that as "the file is gone" and silently fall
back to a different file (the bug ADR-109 fixes). This module answers one
question — *can I read this file right now?* — and makes a best-effort attempt
to bring an evicted file back, before the caller escalates to an explicit
recovery dialog.

Everything here is best-effort and provider-agnostic. The single platform branch
in the whole change lives in :func:`request_download` (an ``brctl`` call on
macOS); the rest is plain filesystem probing that works the same everywhere. No
provider SDKs, no per-vendor APIs — just "is it readable, and if not, nudge it
and wait a bounded while."
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from mfl_desktop import sandbox


def icloud_placeholder(path: Path | str) -> Optional[Path]:
    """The iCloud eviction placeholder sibling for ``path``, if present.

    When iCloud Drive evicts a file ``Foo.mfl`` it replaces it with a hidden,
    tiny ``.Foo.mfl.icloud`` placeholder in the same folder. Its presence is a
    strong, macOS-specific signal that the real file exists in the user's
    account but isn't materialised locally yet — exactly the case where we want
    to request a download rather than declare the file missing. Returns the
    placeholder path if it exists, else ``None`` (no placeholder, or a provider
    that doesn't use this convention)."""
    path = Path(path)
    candidate = path.with_name(f".{path.name}.icloud")
    return candidate if candidate.exists() else None


def _readable(path: Path) -> bool:
    """True if a single byte can actually be read from ``path``.

    Stronger than ``exists()``: an evicted cloud placeholder can report a size
    and pass ``exists()`` yet fail (or block) on read. We open and read one byte
    so we only ever report available when the bytes are really here. A zero-byte
    file (a freshly-created placeholder) is treated as not-yet-available."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with open(path, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False


def is_available(path: Path | str) -> bool:
    """True if ``path`` is a real, locally-readable file right now.

    Non-blocking: returns promptly. Use :func:`ensure_available` when you want to
    wait for / trigger a cloud download."""
    return _readable(Path(path))


def request_download(path: Path | str) -> None:
    """Best-effort nudge to materialise an evicted cloud file. Never raises.

    Two provider-agnostic levers, both swallowed on any failure:

    - **macOS / iCloud:** if an ``.icloud`` placeholder is present, ask the
      File Provider to fetch it via ``brctl download`` (no-op / harmless if
      ``brctl`` is missing or the file isn't an iCloud file). **Skipped under the
      macOS App Sandbox** (ADR-125): a sandboxed app can't spawn a helper
      executable, so we fall straight through to the generic read-to-hydrate
      below — which works for the file we hold access to (the bookmarked working
      file, or a powerbox-picked file).
    - **Everywhere else (OneDrive / Dropbox / Google Drive on Windows, etc.) —
      and the sandboxed-macOS case:** simply *opening* a placeholder is what
      triggers on-demand hydration, so a one-byte read kicks the provider into
      fetching the bytes. The read itself may block briefly; callers run this off
      the UI thread (see :func:`ensure_available`)."""
    path = Path(path)
    try:
        if (
            sys.platform == "darwin"
            and not sandbox.is_sandboxed()
            and icloud_placeholder(path) is not None
        ):
            subprocess.run(
                ["brctl", "download", str(path)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            return
        # Generic hydration trigger: touch the bytes.
        with open(path, "rb") as fh:
            fh.read(1)
    except Exception:
        # Best-effort only — the caller will retry / show the recovery dialog.
        pass


def ensure_available(
    path: Path | str,
    *,
    timeout_s: float = 10.0,
    poll_s: float = 0.4,
    pump: Optional[Callable[[], None]] = None,
) -> bool:
    """Try to make ``path`` locally readable within ``timeout_s``.

    Returns True as soon as the file is readable, else False on timeout. On the
    first miss it fires :func:`request_download` once (so an evicted cloud file
    starts hydrating), then polls every ``poll_s`` until the file appears or the
    budget runs out.

    The blocking probe runs on a daemon thread so a stalled placeholder can't
    freeze the caller, and ``pump`` (typically ``QApplication.processEvents``)
    is called each tick so a splash/UI keeps painting during the wait. Pure
    apart from the injected ``pump`` — no Qt import here.

    ``time.monotonic`` is used for the deadline (not wall-clock), so it's immune
    to clock changes mid-wait."""
    path = Path(path)
    if _readable(path):
        return True

    # Kick a download off the calling thread — it may block on a stubborn
    # placeholder, and we still want to pump the UI while it runs.
    threading.Thread(target=request_download, args=(path,), daemon=True).start()

    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if pump is not None:
            try:
                pump()
            except Exception:
                pass
        if _readable(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.01, poll_s))
