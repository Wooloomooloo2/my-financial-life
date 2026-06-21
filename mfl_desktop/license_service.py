"""Licensing orchestration (ADR-079) — the thin layer that binds the pure
:mod:`mfl_desktop.licensing` logic to the app's persisted state.

Keeps the date/clock and the :mod:`mfl_desktop.app_session` (QSettings)
persistence out of the pure module so that stays trivially testable. Nothing
here touches Qt widgets — the UI calls :func:`current_status` to render and
:func:`apply_license_key` to install a pasted key.
"""
from __future__ import annotations

from datetime import date

from mfl_desktop import app_session, licensing
from mfl_desktop.licensing import LicenseError, LicenseInfo, LicenseStatus
from mfl_desktop.version import APP_EDITION

# Where the About / Enter-License dialogs send the user to purchase. Placeholder
# until the W1 marketing site is live (RELEASE_1.0_BACKLOG workstream W); kept
# here as the single source so every surface links to the same place.
BUY_URL = "https://myfinancial.life/buy"


def ensure_trial_started(today: date | None = None) -> str:
    """Return the trial start date (ISO), recording *today* the first time
    this is ever called on the machine. First-write-wins, so the trial clock
    can't be reset by reinstalling/relaunching."""
    existing = app_session.get_trial_start()
    if existing:
        return existing
    start = (today or date.today()).isoformat()
    app_session.set_trial_start(start)
    return start


def current_status(today: date | None = None) -> LicenseStatus:
    """The resolved :class:`LicenseStatus` for this launch — the one call the
    UI needs. Starts the trial clock if it hasn't begun."""
    now = today or date.today()
    trial_start = ensure_trial_started(now)
    key = app_session.get_license_key()
    return licensing.evaluate(key, trial_start, now, APP_EDITION)


def apply_license_key(key_str: str) -> LicenseInfo:
    """Verify a pasted key and, if good and edition-covering, install it.

    Raises :class:`LicenseError` (user-safe message) for a malformed/forged
    key or one that doesn't cover this major version — without persisting it,
    so a bad paste never displaces a working key."""
    info = licensing.parse_and_verify(key_str)
    if not licensing.edition_covers(info, APP_EDITION):
        raise LicenseError(
            f"This key covers version {info.edition}.x, but this is version "
            f"{APP_EDITION}.x. A version {APP_EDITION} key is required."
        )
    app_session.set_license_key(info.raw)
    return info


def remove_license() -> None:
    """Clear the installed key (reverts to trial/expired). Used by tests and a
    possible 'remove license' affordance."""
    app_session.set_license_key(None)
