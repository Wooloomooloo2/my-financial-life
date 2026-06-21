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
