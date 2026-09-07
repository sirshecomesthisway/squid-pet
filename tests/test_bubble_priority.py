"""Bubble priority (Pink-2026-09-07): _pending_bubble is a single
last-writer-wins slot shared by the watcher thread (state transitions) and
the JS-RPC mood thread (drowsy/waking). On wake-from-sleep both fire within
~1-2s -- the mood "waking" emote used to clobber the state "why" bubble
("claude's mid-turn" / "claude ran a command") before the ~800ms frontend
poll ever saw it, so the meaningful line was silently dropped.

Fix: writes carry a priority. A state-transition "why" bubble outranks a
generic mood emote, so it survives the race regardless of write order.
"""
from __future__ import annotations

import threading

from squid_pet.watcher import PetState
from squid_pet.window import (
    PetApi,
    BUBBLE_PRIO_MOOD,
    BUBBLE_PRIO_STATE,
)


class _StubObserver:
    def __init__(self, mood_bubble="*stretches*", state_bubble="claude's mid-turn"):
        self.mood_bubble = mood_bubble
        self.state_bubble = state_bubble

    def on_mood_change(self, old, new):
        return self.mood_bubble if new else None

    def on_state_change(self, old, new, **kw):
        return self.state_bubble if old != new else None

    def on_interaction(self, kind):
        return None

    def on_still_working(self, cmd):
        return None


def _make_api(observer=None) -> PetApi:
    api = PetApi.__new__(PetApi)
    api._lock = threading.Lock()
    api._latest = PetState(state="idle")
    api._forced_state = None
    api._passthrough = None
    api._observer = observer or _StubObserver()
    api._sm = None
    api._pending_bubble = None
    api._pending_bubble_priority = BUBBLE_PRIO_MOOD
    api._frontend_mood = ""
    api._last_state_for_bubble = "idle"
    api._last_working_bubble_at = 0.0
    api._last_working_bubble_text = ""
    return api


def test_mood_bubble_does_not_clobber_pending_state_why_bubble():
    """A state 'why' bubble is already pending; the wake 'stretch' mood
    change must NOT overwrite it -- this is the dropped-bubble bug."""
    api = _make_api()
    api._pending_bubble = "claude's mid-turn"
    api._pending_bubble_priority = BUBBLE_PRIO_STATE
    api._frontend_mood = "sleeping"

    api.notify_mood("stretch")  # would emit a low-priority "waking" emote

    assert api._pending_bubble == "claude's mid-turn"


def test_state_why_bubble_replaces_pending_mood_bubble():
    """The reverse order: a mood 'waking' emote is pending, then a real
    state transition fires -- the 'why' bubble must win."""
    api = _make_api()
    api._pending_bubble = "*stretches*"
    api._pending_bubble_priority = BUBBLE_PRIO_MOOD
    api._last_state_for_bubble = "sleeping"

    api.update(PetState(state="working"))  # sleeping -> working transition

    assert api._pending_bubble == "claude's mid-turn"


def test_equal_priority_still_last_writer_wins():
    """Two state-level bubbles: later write replaces the earlier one, so
    within a priority level behavior is unchanged from today."""
    api = _make_api()
    api._set_pending_bubble("first", BUBBLE_PRIO_STATE)
    api._set_pending_bubble("second", BUBBLE_PRIO_STATE)
    assert api._pending_bubble == "second"


def test_any_bubble_lands_when_slot_empty():
    """An empty slot accepts any priority, so a lone mood bubble still
    shows when nothing outranks it."""
    api = _make_api()
    api._set_pending_bubble("*yawn*", BUBBLE_PRIO_MOOD)
    assert api._pending_bubble == "*yawn*"
