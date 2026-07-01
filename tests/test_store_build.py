"""Store-build licensing dormancy (ADR-125, increment F).

In an app-store build the store owns the purchase + entitlement, so the ADR-079
offline-key + 30-day trial must go dormant. This pins:

- ``version.is_store_build()`` resolution — the stamped ``_build_info.STORE_BUILD``
  flag wins, else it falls back to ``sandbox.is_sandboxed()`` (the MAS build is
  the only sandboxed one), else False in a dev checkout; and
- ``license_service.current_status()`` short-circuits to an unlocked, owned
  status **without** starting the trial clock when it's a store build — the
  single chokepoint every UI surface (launch nag, title cue, About box) reads.

Imports ``license_service`` (→ ``app_session`` → PySide6), so run under the
miniforge interpreter:

    /opt/homebrew/Caskroom/miniforge/base/bin/python3 tests/test_store_build.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import mfl_desktop as _pkg
from mfl_desktop import version, sandbox, license_service


_BUILD_INFO = "mfl_desktop._build_info"


def _set_build_info(module_or_none):
    """Override (or remove) the ``mfl_desktop._build_info`` module for a test.

    ``from mfl_desktop import _build_info`` resolves the *package attribute*
    first (and a real, gitignored ``_build_info.py`` may exist on disk), so we
    patch both the package attribute and the sys.modules entry, and restore both.
    Returns a restore callable."""
    prior_sys = sys.modules.get(_BUILD_INFO, None)
    had_attr = hasattr(_pkg, "_build_info")
    prior_attr = getattr(_pkg, "_build_info", None)

    if module_or_none is None:
        # Setting the sys.modules entry to None (not popping it) makes
        # ``from mfl_desktop import _build_info`` raise ImportError instead of
        # re-reading a real, gitignored ``_build_info.py`` off disk — which a
        # local store build (``build_mas.sh --store``) leaves behind with
        # STORE_BUILD=True and would otherwise leak back into "no build info".
        sys.modules[_BUILD_INFO] = None
        if had_attr:
            delattr(_pkg, "_build_info")
    else:
        sys.modules[_BUILD_INFO] = module_or_none
        setattr(_pkg, "_build_info", module_or_none)

    def restore():
        if prior_sys is None:
            sys.modules.pop(_BUILD_INFO, None)
        else:
            sys.modules[_BUILD_INFO] = prior_sys
        if had_attr:
            setattr(_pkg, "_build_info", prior_attr)
        elif hasattr(_pkg, "_build_info"):
            delattr(_pkg, "_build_info")
    return restore


def _fake_build_info(**attrs):
    m = types.ModuleType(_BUILD_INFO)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _patch(obj, name, value):
    saved = getattr(obj, name)
    setattr(obj, name, value)
    return lambda: setattr(obj, name, saved)


# ── version.is_store_build ───────────────────────────────────────────────────


def test_store_build_false_in_dev():
    restore_bi = _set_build_info(None)               # no _build_info
    restore_sb = _patch(sandbox, "is_sandboxed", lambda: False)
    try:
        assert version.is_store_build() is False
    finally:
        restore_sb(); restore_bi()


def test_store_build_from_stamped_flag():
    restore_bi = _set_build_info(_fake_build_info(STORE_BUILD=True))
    # Even if not sandboxed, the stamped flag wins.
    restore_sb = _patch(sandbox, "is_sandboxed", lambda: False)
    try:
        assert version.is_store_build() is True
    finally:
        restore_sb(); restore_bi()


def test_store_build_falls_back_to_sandbox():
    restore_bi = _set_build_info(_fake_build_info())   # _build_info but no flag
    restore_sb = _patch(sandbox, "is_sandboxed", lambda: True)
    try:
        assert version.is_store_build() is True
    finally:
        restore_sb(); restore_bi()


def test_stamped_flag_false_falls_back_to_sandbox():
    restore_bi = _set_build_info(_fake_build_info(STORE_BUILD=False))
    restore_sb = _patch(sandbox, "is_sandboxed", lambda: False)
    try:
        assert version.is_store_build() is False
    finally:
        restore_sb(); restore_bi()


# ── license_service.current_status chokepoint ────────────────────────────────


def test_current_status_store_build_owned_and_skips_trial():
    """Store build → unlocked LICENSED, and the trial clock is never started."""
    trial_calls = []
    restore_store = _patch(license_service, "is_store_build", lambda: True)
    restore_trial = _patch(
        license_service, "ensure_trial_started",
        lambda today=None: trial_calls.append(1) or "x",
    )
    try:
        st = license_service.current_status()
        assert st.unlocked is True
        assert st.state == "licensed"
        assert "App Store" in st.message
        assert trial_calls == []      # short-circuited before any trial write
    finally:
        restore_trial(); restore_store()


def test_current_status_non_store_still_evaluates():
    """When not a store build the normal evaluate path runs (trial clock used)."""
    seen = {}

    def _fake_trial(today=None):
        seen["trial"] = True
        return "2026-06-30"

    def _fake_evaluate(*a, **k):
        seen["evaluated"] = True
        return "STATUS"

    restore_store = _patch(license_service, "is_store_build", lambda: False)
    restore_trial = _patch(license_service, "ensure_trial_started", _fake_trial)
    restore_eval = _patch(license_service.licensing, "evaluate", _fake_evaluate)
    try:
        out = license_service.current_status()
        assert out == "STATUS"
        assert seen == {"trial": True, "evaluated": True}
    finally:
        restore_eval(); restore_trial(); restore_store()


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
