"""Single source of truth for the application version (ADR-079).

``__version__`` is surfaced in the About box, the window title's tooltip, and
crash/diagnostic output, and drives the **edition entitlement** check: a 1.x
license key unlocks every 1.x build, and 2.0 will be a new paid key (ADR-079
pricing decision C1). ``APP_EDITION`` is the integer major version a license
must cover to unlock this build — derived from ``__version__`` so the two can
never drift.
"""
from __future__ import annotations

__version__ = "1.0.0"
APP_NAME = "My Financial Life"

# The major version a license must entitle to unlock this build (ADR-079).
APP_EDITION = int(__version__.split(".", 1)[0])

# Product links (ADR-098). Single source of truth for the website + docs URLs
# the in-app Help menu and onboarding point at. The site lives on the
# Garelochsoft company domain (Garelochsoft also publishes My Retirement Life);
# routes are flat to match the live Astro site (same domain as
# license_service.BUY_URL).
WEBSITE_URL = "https://garelochsoft.com"
DOCS_URL = "https://garelochsoft.com/docs/getting-started"


def build_revision() -> str:
    """A short build identifier surfaced in About + diagnostics (ADR-099).

    A packaged build's CI step writes an optional ``mfl_desktop/_build_info.py``
    with ``REVISION`` (e.g. a git short SHA) and ``BUILD_DATE``; this reads it
    if present. A plain source checkout has no such file, so it falls back to
    ``"source"`` — never runs git at runtime (fragile in a frozen app)."""
    try:
        from mfl_desktop import _build_info  # type: ignore
    except Exception:
        return "source"
    rev = getattr(_build_info, "REVISION", "") or "source"
    return str(rev)


def build_string() -> str:
    """``"1.0.0 (source)"`` — version + build revision, for one-line display."""
    return f"{__version__} ({build_revision()})"
