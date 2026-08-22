"""Single source of truth for the application version.

``__version__`` is surfaced in the About box, the window title's tooltip, and
crash/diagnostic output. It carried an ``APP_EDITION`` entitlement constant and
an ``is_store_build()`` flag until ADR-193 removed licensing — the app is free
and always fully unlocked, so neither has a consumer left.
"""
from __future__ import annotations

__version__ = "1.0.0"
APP_NAME = "My Financial Life"

# Product links (ADR-098). Single source of truth for the website + docs URLs
# the in-app Help menu and onboarding point at. The site lives on the
# Garelochsoft company domain (Garelochsoft also publishes My Retirement Life);
# routes are flat to match the live Astro site.
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
