"""The native drag loop shares the passthrough loop's autorelease leak
(2026-09-06 follow-up to the passthrough fix).

`window._native_drag_loop` polls NSEvent at 60Hz on a worker thread while a
drag is in progress, calling `NSEvent.pressedMouseButtons()` /
`NSEvent.mouseLocation()` (autoreleased Cocoa objects) each tick with no
NSRunLoop to drain them. It only runs during an active drag, so it leaks far
less than the passthrough loop did -- but it's the same pattern, so it gets
the same per-tick `objc.autorelease_pool()` wrap.

Removing the `with objc.autorelease_pool()` wrapper makes this fail.
"""
from __future__ import annotations

import contextlib

import pytest

objc = pytest.importorskip("objc")
pytest.importorskip("AppKit")

from squid_pet import window as W


def test_native_drag_loop_opens_autorelease_pool_each_tick(monkeypatch):
    entries = {"n": 0}

    @contextlib.contextmanager
    def counting_pool():
        entries["n"] += 1
        yield

    monkeypatch.setattr(objc, "autorelease_pool", counting_pool)
    monkeypatch.setattr("squid_pet.window._time.sleep", lambda _s: None)
    # Don't dispatch to the AppKit main thread (no runloop under pytest).
    monkeypatch.setattr("PyObjCTools.AppHelper.callAfter", lambda *a, **k: None)
    # Clamp is screen-dependent; identity keeps the tick pure.
    monkeypatch.setattr("squid_pet.window.clamp_origin_to_screen", lambda x, y: (x, y))

    ticks = 5
    calls = {"n": 0}

    class _FakeLoc:
        x = 100.0
        y = 100.0

    class _FakeNSEvent:
        @staticmethod
        def pressedMouseButtons():
            # Left button held for `ticks` iterations, then released -> break.
            calls["n"] += 1
            return 1 if calls["n"] <= ticks else 0

        @staticmethod
        def mouseLocation():
            return _FakeLoc()

    monkeypatch.setattr("AppKit.NSEvent", _FakeNSEvent)

    class _FakeFrame:
        class origin:
            x = 100.0
            y = 100.0

    class _FakeWindow:
        def frame(self):
            return _FakeFrame()

        def setFrameOrigin_(self, _p):
            pass

    monkeypatch.setattr("squid_pet.window._get_ns_window", lambda: _FakeWindow())

    W._drag_stop.clear()
    W._native_drag_loop((100.0, 100.0), (100.0, 100.0), passthrough=None, on_end=lambda: None)

    assert calls["n"] > ticks, "drag loop never iterated"
    assert entries["n"] >= ticks, (
        f"autorelease pool opened {entries['n']} times over {calls['n']} drag "
        "ticks -- the drag loop is not wrapping each iteration in a pool"
    )
