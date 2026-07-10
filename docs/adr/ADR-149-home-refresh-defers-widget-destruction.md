# ADR-149 — Home refresh defers widget destruction (mouse-event use-after-free)

**Date:** 2026-07-10
**Status:** Implemented
**Related:** ADR-075 (Home dashboard as stack page 0). ADR-119 (net-worth hero + clickable cards). ADR-063 (Schedules cue refreshed on window activation). ADR-023 (Schedules dialog).

## Context

Owner report: the app crashed with `EXC_BAD_ACCESS (SIGSEGV)` — the first hard crash it has ever produced. The faulting address was `0xffffffffffff11c0`, a small negative offset from null, and the crashing frame was `QApplication::notify` on the main thread, reached from `QApplicationPrivate::sendMouseEvent`:

```
0  QtWidgets  QApplication::notify + 5148          ← crash
...
4  QtWidgets  QApplicationPrivate::sendMouseEvent
7  QtWidgets  QApplicationPrivate::notify_helper
8  QtWidgets  QApplication::notify
11 QtGui      QGuiApplicationPrivate::processMouseEvent
```

`notify_helper` — which calls the receiver widget's `event()`, and therefore our Python `mousePressEvent` — had already **returned** by the time of the fault. Qt was still using the receiver widget afterwards. The receiver had been freed during its own event handler: a classic use-after-free, and one Python can neither catch nor report, since the object is destroyed on the C++ side while a raw pointer to it is live on Qt's stack.

The application log showed the Home dashboard being rebuilt (each rebuild recomputes holdings and logs an `oversold …` line per security) twice in the final second before the fault.

`QScrollArea::setWidget` **deletes the widget it replaces, immediately** — not via `deleteLater`. `HomeView.refresh()` rebuilt the entire dashboard that way:

```python
self._scroll.setWidget(container)   # old container + every card deleted, now
```

And `RegisterWindow.changeEvent` refreshes Home whenever the window regains activation and Home is the visible page (ADR-063/075). Combine that with a card whose clicked slot opens a **modal** dialog, and the dashboard destroys itself from inside one of its own cards' mouse events:

```
_Card.mousePressEvent                       # Qt holds a raw pointer to this card
  └─ clicked.emit()
      └─ RegisterWindow._on_manage_schedules
          └─ SchedulesDialog.exec()         # nested event loop
              └─ (dialog closes, register window re-activates)
                  └─ RegisterWindow.changeEvent → ActivationChange
                      └─ HomeView.refresh()
                          └─ QScrollArea.setWidget(new) → delete old card
  ← mousePressEvent returns into QApplication::notify, which dereferences the
    freed card. SIGSEGV.
```

`_Card`, `_Row` and `_AccordionHeader` each carried a comment claiming safety because they call `super().mousePressEvent(e)` *before* `clicked.emit()`, so as never to touch `self` after the slot may have deleted it. That ordering is necessary but **not sufficient**, and the comments gave a false sense of safety: the dangling dereference is inside Qt's own code after our handler returns, and no ordering within the handler can prevent it.

Reproduced deterministically offscreen: a real `_Card` in a `QScrollArea`, a clicked slot that runs `QDialog.exec()` then `setWidget(new)`, driven by `QTest.mouseClick` → exit code 139.

This is why it took until now to surface. It needs a click on one of the two Home cards whose slot is modal (Bills/Schedules), while Home is the visible page — the report cards open non-modal singleton windows and never re-enter activation inside the click.

It needs one more thing besides: the `ActivationChange` must be delivered *synchronously, inside the modal's nested loop*, while the click is still on the stack. Delivered after the click unwinds, `refresh()` frees a card nobody points at any more and nothing happens. The owner reports the app was **full-screen** when it crashed, and the report shows a Cocoa window animation live on another thread at the instant of the fault:

```
Thread 4:: Dispatch queue: com.apple.root.user-interactive-qos
8   AppKit   -[NSAnimation _runBlocking] + 412
```

macOS full-screen is built on space transitions and `NSAnimation`, and the app contains no full-screen-specific code at all (no `WindowStateChange` handler, one `changeEvent` in the codebase), so full-screen cannot have changed *what* ran — only *when* Cocoa posted the activation. That is exactly the variable this crash turns on. Unproven: reproducing it needs a real display and a real space transition, which the offscreen platform plugin cannot model. Recorded because it is the likeliest reason a latent bug waited this long, and because the fix below is deliberately independent of that timing.

## Decision

**`HomeView.refresh()` must not destroy the old container synchronously.** Take it out of the scroll area first, then let the event loop delete it once Qt has finished with the events in flight:

```python
old = self._scroll.takeWidget()     # relinquishes ownership without deleting
self._scroll.setWidget(container)
if old is not None:
    old.deleteLater()
```

`takeWidget()` hands ownership back to the caller rather than deleting, and `deleteLater()` posts a `DeferredDelete` that a running event loop delivers only after the current event has been fully dispatched — and, when posted from inside a modal dialog's nested loop, only after that loop exits. Either way the card outlives every stack frame that still points at it.

This is a single-point fix at the only site that rebuilds a live widget tree repeatedly. The other four `setWidget` calls in the codebase (`budget_monthly_view`, `spending_report_window`, `investment_returns_window`, `import_category_review_dialog`) all run once during construction, with no prior widget to delete and no event in flight.

The three misleading `mousePressEvent` comments are corrected in place to say that the ordering is necessary but not sufficient, and to point here for the invariant that actually holds.

Rejected:

- **Deferring the `clicked` emit** (`QTimer.singleShot(0, self.clicked.emit)`) in each of the three widgets. Fixes the symptom at three call sites while leaving the loaded gun — `refresh()` freeing arbitrary live widgets — pointed at every future one. It also makes navigation lag a frame behind the click.
- **Guarding `changeEvent` with a "not during a mouse event" flag.** Fragile, and the same crash returns via any other synchronous refresh path.
- **Making the Schedules dialog non-modal.** Treats one instance of a general defect, and modality is the right interaction there.
- **Keeping a Python reference to the old container.** Irrelevant — `setWidget` destroys the *C++* object regardless of what Python holds; the wrapper simply goes invalid.

## Consequences

- Clicking the Bills/Schedules card on Home, closing the dialog, and returning to a dashboard that refreshes under the click no longer crashes the app.
- Home's previous container survives one extra turn of the event loop before being freed. It is detached from the scroll area and unpainted for that interval, so the only cost is a transient double-hold of one dashboard's widgets.
- The general invariant is now stated where it belongs: **a widget tree that can be rebuilt from a slot must never be torn down synchronously**, because any widget in it may be the receiver of the event currently being dispatched. Worth applying to any future surface that rebuilds itself on activation.
- `deleteLater()` requires a running event loop. Refreshes that happen before `app.exec()` (e.g. `refresh_after_first_run`) queue their deletion until the loop starts, which is correct and was verified in test.
- `tests/test_home_refresh_use_after_free.py` 3/3, each in a subprocess and judged on exit code, since the regression *crashes the interpreter* rather than failing an assert: the raw pattern still segfaults Qt 6.11 (pinning the mechanism, so the guard can be revisited if Qt ever changes); the real `HomeView.refresh()` survives repeated real clicks on real cards through a modal dialog; and `refresh()` leaves the old container alive on return but destroyed after one loop turn (no leak). Reverting the fix drops this file to 1/3. Full suite 39/39. No schema change.
