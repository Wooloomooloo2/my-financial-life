"""Home refresh must not free a card that is mid-mouse-event (ADR-149).

``QScrollArea::setWidget`` deletes the widget it replaces *immediately*. Home's
refresh() rebuilt the whole card container that way, and refresh() can run while
one of those very cards is still on the stack delivering a mouse press:

    _Card.mousePressEvent
      └─ clicked.emit()
          └─ RegisterWindow._on_manage_schedules
              └─ SchedulesDialog.exec()          # nested event loop
                  └─ (dialog closes, window re-activates)
                      └─ RegisterWindow.changeEvent  → ActivationChange
                          └─ HomeView.refresh()
                              └─ QScrollArea.setWidget(new)  → delete old card

When the click unwound, Qt's QApplication::notify went on using the freed
receiver widget → EXC_BAD_ACCESS / SIGSEGV. Calling ``super().mousePressEvent``
before ``emit`` does not help: the dangling access is inside Qt's own code after
our handler returns.

The fix takes the old container out of the scroll area and defers its
destruction to the event loop, so nothing Qt still holds is freed under it.

These tests *crash the interpreter* rather than fail an assert when the bug is
back, so each runs in a subprocess and is judged on its exit code.

Qt (offscreen) — ``python3 tests/test_home_refresh_use_after_free.py``.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


_PREAMBLE = f"""
import os, sys
sys.path.insert(0, {str(_REPO_ROOT)!r})
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import (
    QApplication, QScrollArea, QWidget, QVBoxLayout, QDialog,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
app = QApplication([])
"""


def _run(body: str) -> subprocess.CompletedProcess:
    """Run a Qt snippet in a subprocess; a segfault shows up as a negative
    returncode (-11) instead of taking this test process down with it."""
    return subprocess.run(
        [sys.executable, "-c", _PREAMBLE + textwrap.dedent(body)],
        capture_output=True, text=True, timeout=120,
    )


def test_deleting_the_clicked_card_synchronously_would_crash():
    """Pin the mechanism itself: if the receiver of a mouse press is destroyed
    while the press is still unwinding, Qt segfaults. If this ever stops
    crashing, Qt changed and the guard below may be relaxed.

    Retried, because a use-after-free is *undefined behaviour*, not a guaranteed
    fault: sometimes the freed memory is still mapped and readable and the click
    unwinds cleanly. A single attempt made this canary fail intermittently under
    CPU load (ADR-153). "Crashes at least once in N tries" is the honest form of
    the claim — if Qt ever really did make this safe, every attempt would survive
    and the assertion would still fire."""
    body = """
        from mfl_desktop.ui.home_view import _Card
        scroll = QScrollArea(); scroll.setWidgetResizable(True)

        def build():
            c = QWidget(); lay = QVBoxLayout(c)
            card = _Card("BILLS", action="Schedules")
            card.make_clickable(); card.setMinimumHeight(80)
            lay.addWidget(card); lay.addStretch(1)
            card.clicked.connect(on_click)
            return c, card

        def on_click():
            d = QDialog(); QTimer.singleShot(0, d.accept); d.exec()
            new_c, _ = build()
            scroll.setWidget(new_c)      # deletes the old container NOW

        c, card = build()
        scroll.setWidget(c); scroll.resize(400, 300); scroll.show()
        app.processEvents()
        QTest.mouseClick(card, Qt.LeftButton)
        print("SURVIVED")
    """
    attempts = [_run(body) for _ in range(5)]
    crashed = [r for r in attempts if r.returncode != 0 and "SURVIVED" not in r.stdout]
    assert crashed, (
        "the unguarded pattern survived all 5 attempts; Qt may have changed and "
        "the ADR-149 guard could be revisited "
        f"(returncodes={[r.returncode for r in attempts]})"
    )


def test_real_home_refresh_survives_a_click_that_rebuilds_it():
    """The real HomeView.refresh(), driven by a real click on a real card whose
    slot opens a modal dialog — exactly the crashed call stack."""
    r = _run("""
        import tempfile, pathlib, shiboken6
        from mfl_desktop.db.repository import Repository
        from mfl_desktop.ui.home_view import HomeView

        tmp = pathlib.Path(tempfile.mkdtemp()) / "t.mfl"
        repo = Repository(tmp)
        repo.create_account(name="Chk", type_key="cash", currency="GBP")

        home = HomeView(repo)
        home.resize(900, 700); home.show()
        home.refresh()
        app.processEvents()

        # Reproduce _on_manage_schedules: modal exec inside the clicked slot,
        # then the ActivationChange handler's refresh of the visible Home page.
        def on_schedules():
            d = QDialog(); QTimer.singleShot(0, d.accept); d.exec()
            home.refresh()
        home.schedules_requested.connect(on_schedules)

        def live_cards():
            # Re-queried before EVERY click, never snapshotted. A click that
            # rebuilds Home destroys the other cards, and so does the ADR-150
            # background pass when it lands mid-loop; iterating a stale list
            # would click a freed wrapper and raise RuntimeError. That is a bug
            # in the test, not the use-after-free this file is about — and it
            # made the test fail intermittently under CPU load (ADR-153).
            return [w for w in home.findChildren(QWidget)
                    if type(w).__name__ == "_Card"
                    and getattr(w, "_clickable", False)
                    and shiboken6.isValid(w)]

        assert live_cards(), "no clickable card found on Home"

        # Click every clickable card; each rebuilds Home under its own feet.
        for _ in range(3):
            for i in range(len(live_cards())):
                cards = live_cards()
                if i >= len(cards):
                    break            # the rebuild left fewer cards than before
                QTest.mouseClick(cards[i], Qt.LeftButton)
                app.processEvents()
        print("SURVIVED")
    """)
    assert r.returncode == 0, (
        f"HomeView.refresh() crashed (rc={r.returncode})\n"
        f"stdout={r.stdout}\nstderr={r.stderr[-2000:]}"
    )
    assert "SURVIVED" in r.stdout


def test_refresh_defers_destruction_of_the_old_container():
    """refresh() must not leave the previous container destroyed on return —
    that is the property the fix relies on."""
    r = _run("""
        import tempfile, pathlib, shiboken6
        from mfl_desktop.db.repository import Repository
        from mfl_desktop.ui.home_view import HomeView

        tmp = pathlib.Path(tempfile.mkdtemp()) / "t.mfl"
        repo = Repository(tmp)
        repo.create_account(name="Chk", type_key="cash", currency="GBP")

        home = HomeView(repo)
        home.refresh()
        first = home._container
        assert shiboken6.isValid(first)

        home.refresh()                      # rebuild
        # Still alive right after refresh(): destruction is deferred.
        assert shiboken6.isValid(first), "old container was deleted synchronously"

        # DeferredDelete is only delivered by a running event loop (which the
        # real app always has), not by a bare processEvents().
        QTimer.singleShot(0, app.quit)
        app.exec()
        assert not shiboken6.isValid(first), "old container was never deleted (leak)"
        print("SURVIVED")
    """)
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr[-2000:]}"
    assert "SURVIVED" in r.stdout


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
