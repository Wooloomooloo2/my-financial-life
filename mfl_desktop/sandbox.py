"""macOS App Sandbox foundation — detection + security-scoped bookmarks (ADR-125).

The Mac App Store build runs inside Apple's **App Sandbox**, which only lets the
app reach files the user explicitly picks (via the open/save panel = the
"powerbox") for the duration of that pick. To re-open a user's ``.mfl`` on a
*later* launch the app must persist a **security-scoped bookmark** of the URL and
resolve it next time — a plain stored path is simply inaccessible under the
sandbox. Qt exposes no bookmark API, so this is the one place we drop to native
macOS via PyObjC (``pyobjc-framework-Cocoa``, a macOS-only runtime dep).

Increment A of the ADR-125 arc: this module is the self-contained foundation the
rest of the arc builds on (``app_session`` / ``launch`` persistence in B, sidecar
relocation in C, degradation in D). It is written to **degrade to plain paths**
whenever it can't do better — not macOS, PyObjC missing, not sandboxed, or any
native call failing — so development runs (``python -m mfl_desktop``, always
unsandboxed) and the non-macOS builds are completely unaffected. Nothing here
raises; every native path is best-effort and falls back.

Two layers:

- **Primitives** — :func:`create_security_scoped_bookmark` /
  :func:`resolve_security_scoped_bookmark` wrap the raw PyObjC calls and return
  ``None`` on any failure. Usable directly (and unit-tested) on any Mac.
- **Policy** — :func:`encode_location` / :func:`decode_location` apply the
  ADR-125 rule: persist a real bookmark **only when actually sandboxed**, else a
  tagged plain path. The persisted token is a single string suited to
  ``QSettings``; :func:`decode_location` also accepts a bare legacy path so the
  pre-sandbox ``session/last_db_path`` value keeps resolving.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# NSURL bookmark option bit-flags (stable Foundation constants). Hard-coded so we
# don't depend on PyObjC exporting the names; matched against Apple's headers:
#   NSURLBookmarkCreationWithSecurityScope    = 1 << 11
#   NSURLBookmarkResolutionWithSecurityScope  = 1 << 10
_CREATE_WITH_SECURITY_SCOPE = 1 << 11
_RESOLVE_WITH_SECURITY_SCOPE = 1 << 10

# Persisted-token tags (see encode_location / decode_location).
_BOOKMARK_PREFIX = "bm:"
_PLAIN_PREFIX = "path:"


def is_sandboxed() -> bool:
    """True when running inside the macOS App Sandbox.

    Apple sets ``APP_SANDBOX_CONTAINER_ID`` in the environment of every
    sandboxed process; its presence is the canonical, cheap signal. A normal
    development run (or any non-macOS build) never has it, so this returns
    False there and the whole module quietly falls back to plain paths."""
    return sys.platform == "darwin" and bool(
        os.environ.get("APP_SANDBOX_CONTAINER_ID")
    )


def bookmarks_supported() -> bool:
    """True when security-scoped bookmarks can actually be created here — i.e.
    we're on macOS and PyObjC's Foundation bridge imports. Independent of
    :func:`is_sandboxed` (the primitives work unsandboxed too, which is what
    lets them be unit-tested); the *policy* layer is what gates on the sandbox."""
    if sys.platform != "darwin":
        return False
    try:
        import Foundation  # noqa: F401  (probe only)
    except Exception:
        return False
    return True


# ── primitives ───────────────────────────────────────────────────────────────


def create_security_scoped_bookmark(path: Path | str) -> Optional[str]:
    """Create a security-scoped bookmark for ``path`` and return it base64-encoded
    (a plain ``str`` ready to persist), or ``None`` on any failure / unsupported.

    Never raises. The caller decides *whether* to use this (the policy layer only
    does so when sandboxed); the primitive itself works on any Mac with PyObjC."""
    if not bookmarks_supported():
        return None
    try:
        from Foundation import NSURL

        url = NSURL.fileURLWithPath_(str(Path(path)))
        data, _err = (
            url.bookmarkDataWithOptions_includingResourceValuesForKeys_relativeToURL_error_(
                _CREATE_WITH_SECURITY_SCOPE, None, None, None
            )
        )
        if data is None:
            return None
        return str(data.base64EncodedStringWithOptions_(0))
    except Exception:
        return None


def resolve_security_scoped_bookmark(
    blob: str,
) -> Optional["ResolvedLocation"]:
    """Resolve a base64 security-scoped bookmark back to a :class:`ResolvedLocation`,
    or ``None`` if it can't be resolved (stale-beyond-recovery, unsupported,
    malformed). Never raises.

    The returned location holds the native URL so the caller can bracket file
    access with :meth:`ResolvedLocation.start` / :meth:`ResolvedLocation.stop`
    (or use it as a context manager). ``stale`` is set when macOS resolved the
    bookmark but signalled it should be re-created — the caller can keep using
    ``path`` for this session and re-mint the bookmark next time the user
    picks the file."""
    if not bookmarks_supported():
        return None
    try:
        from Foundation import NSURL, NSData

        nsdata = NSData.alloc().initWithBase64EncodedString_options_(blob, 0)
        if nsdata is None:
            return None
        url, stale, _err = (
            NSURL.URLByResolvingBookmarkData_options_relativeToURL_bookmarkDataIsStale_error_(
                nsdata, _RESOLVE_WITH_SECURITY_SCOPE, None, None, None
            )
        )
        if url is None:
            return None
        return ResolvedLocation(path=Path(str(url.path())), stale=bool(stale), _url=url)
    except Exception:
        return None


class ResolvedLocation:
    """A resolved file location plus its (optional) security-scope handle.

    For a real bookmark, ``_url`` is the native ``NSURL`` and
    :meth:`start` / :meth:`stop` open and close the security-scoped access the
    sandbox requires around any read/write. For a plain path (unsandboxed, or a
    legacy stored path) ``_url`` is ``None`` and start/stop are no-ops, so a
    caller can use the same code path in both worlds:

        with sandbox.decode_location(token) as p:
            repo = Repository(p)

    The context manager returns the resolved :class:`pathlib.Path`."""

    __slots__ = ("path", "stale", "_url", "_accessing")

    def __init__(self, path: Path, stale: bool, _url: object | None) -> None:
        self.path = path
        self.stale = stale
        self._url = _url
        self._accessing = False

    def start(self) -> bool:
        """Begin security-scoped access. Returns True when access is held (or
        when none is needed — a plain path). Best-effort; never raises."""
        if self._url is None:
            return True
        try:
            ok = bool(self._url.startAccessingSecurityScopedResource())
            self._accessing = ok
            return ok
        except Exception:
            return False

    def stop(self) -> None:
        """End security-scoped access if it was started. Idempotent; never raises."""
        if self._url is None or not self._accessing:
            return
        try:
            self._url.stopAccessingSecurityScopedResource()
        except Exception:
            pass
        finally:
            self._accessing = False

    def __enter__(self) -> Path:
        self.start()
        return self.path

    def __exit__(self, *_exc) -> None:
        self.stop()


# ── policy (used by app_session / launch — increment B) ──────────────────────


def encode_location(path: Path | str) -> str:
    """Return a persistable token for ``path`` for storage in ``QSettings``.

    ADR-125 rule: a real security-scoped bookmark (``bm:<base64>``) **only when
    actually sandboxed** — that's the one context where a bare path won't reopen,
    and where the bookmark entitlement is in effect. Otherwise (dev runs, non-
    macOS) a tagged plain path (``path:<abs>``), which is simpler and exactly the
    historical behaviour. Falls back to the plain tag if bookmark creation fails
    for any reason, so encoding never leaves the caller without a usable token."""
    if is_sandboxed():
        blob = create_security_scoped_bookmark(path)
        if blob is not None:
            return _BOOKMARK_PREFIX + blob
    return _PLAIN_PREFIX + str(Path(path).resolve())


def decode_location(token: str) -> Optional[ResolvedLocation]:
    """Resolve a token from :func:`encode_location` back to a :class:`ResolvedLocation`.

    Accepts three forms:

    - ``bm:<base64>``   — a security-scoped bookmark (resolved natively).
    - ``path:<abs>``    — a tagged plain path.
    - ``<abs>``         — a bare path with no tag: the **legacy** pre-sandbox
      ``session/last_db_path`` value, so existing installs keep resolving.

    Returns ``None`` only when a bookmark token can't be resolved at all; a plain
    or legacy path always yields a (no-scope) location regardless of whether the
    file currently exists — existence is the caller's check (the launch resolver
    already probes readability via :mod:`mfl_desktop.cloud`)."""
    if not token:
        return None
    if token.startswith(_BOOKMARK_PREFIX):
        return resolve_security_scoped_bookmark(token[len(_BOOKMARK_PREFIX):])
    if token.startswith(_PLAIN_PREFIX):
        raw = token[len(_PLAIN_PREFIX):]
    else:
        raw = token  # bare legacy path
    return ResolvedLocation(path=Path(raw), stale=False, _url=None)
