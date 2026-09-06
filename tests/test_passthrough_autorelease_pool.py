"""The passthrough poll thread leaked Cocoa memory (2026-09-05).

`PassthroughController._loop` runs ~33Hz on a plain background thread and,
every active tick, calls PyObjC APIs (`_get_ns_window` -> `NSApp.windows()`
iteration, `NSEvent.mouseLocation`, `NSScreen`, `nw.frame()`) that create
*autoreleased* Cocoa objects -- including ObjC-internal fast-enumeration
temporaries with no Python wrapper. The AppKit main thread drains its
autorelease pool once per runloop cycle, but a bare Python `while` loop on a
worker thread has no runloop and no pool, so nothing ever drained. Over ~25h
this accumulated ~740k `__NSFastEnumerationEnumerator` objects and grew the
physical footprint to ~450MB (heap-confirmed).

The fix wraps each loop iteration in `objc.autorelease_pool()` so the
transient Cocoa objects a tick creates are released at the end of that tick.

Those internal temporaries are invisible to PyObjC-level dealloc tracking
(and manipulating autorelease refcounts by hand segfaults), so the honest,
safe, deterministic contract to assert is: the poll loop opens a draining
autorelease pool once per tick. This drives the real loop and counts pool
entries; removing the `with objc.autorelease_pool()` wrapper makes it fail.
"""
from __future__ import annotations

import contextlib

import pytest

objc = pytest.importorskip("objc")
pytest.importorskip("AppKit")

from squid_pet.passthrough import PassthroughController


def test_poll_loop_opens_autorelease_pool_each_tick(monkeypatch):
    # Don't actually sleep POLL_INTERVAL * N -- only pool-wrapping matters.
    monkeypatch.setattr("squid_pet.passthrough.time.sleep", lambda _s: None)

    entries = {"n": 0}

    @contextlib.contextmanager
    def counting_pool():
        entries["n"] += 1
        yield

    monkeypatch.setattr(objc, "autorelease_pool", counting_pool)

    ticks = 50
    calls = {"n": 0}

    def fake_get_ns_window():
        # One call per active tick (passthrough.py's `nw = self._get_ns_window()`).
        # Returning None short-circuits the tick right after the leak site, so we
        # exercise the pool wrapping without needing real windows/cursor state.
        calls["n"] += 1
        if calls["n"] >= ticks:
            controller._stop.set()
        return None

    controller = PassthroughController(fake_get_ns_window)
    controller._loop()  # runs synchronously until _stop is set

    assert calls["n"] == ticks, "loop never reached the leak site"
    assert entries["n"] >= ticks, (
        f"autorelease pool opened {entries['n']} times over {ticks} ticks -- "
        "the poll loop is not wrapping each iteration in an autorelease pool, "
        "so autoreleased Cocoa objects accumulate for the life of the thread"
    )
