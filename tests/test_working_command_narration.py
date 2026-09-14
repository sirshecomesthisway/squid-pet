"""Working-state command narration (Pink-2026-09-13: "talk more -- update
on every new command / action, and periodically every 15s").

Two behaviors are covered:

1. While she STAYS 'working' across ticks (no state transition, so
   on_state_change never re-fires), a NEW shell command must surface its own
   bubble IMMEDIATELY -- bypassing the WORKING_REANNOUNCE_SEC throttle that
   governs the generic "still working" beat. Without this, a run of several
   commands in one turn produced one bubble on entry and then silence until
   the next throttled reannounce.

2. The same command seen again must NOT re-announce (dedup shared with the
   periodic reannounce via _last_working_bubble_text).
"""
from __future__ import annotations

import threading

from squid_pet import observer
from squid_pet.watcher import PetState
from squid_pet.window import (
    PetApi,
    BUBBLE_PRIO_AMBIENT,
    BUBBLE_PRIO_MOOD,
    WORKING_REANNOUNCE_SEC,
)


class _FakeDetector:
    def __init__(self, cmdline=None):
        self.shell_cmdline = cmdline


class _FakeSM:
    def __init__(self, claude_cmd=None):
        self._claude_detector = _FakeDetector(claude_cmd)
        self._codex_detector = None


def _make_api(claude_cmd=None) -> PetApi:
    api = PetApi.__new__(PetApi)
    api._lock = threading.Lock()
    api._latest = PetState(state="working")
    api._forced_state = None
    api._passthrough = None
    # Real observer -- we want the actual _shell_cmd_bubble enrichment.
    api._observer = observer.Observer(get_muted=lambda: False)
    api._sm = _FakeSM(claude_cmd)
    api._pending_bubble = None
    api._pending_bubble_priority = BUBBLE_PRIO_MOOD
    api._frontend_mood = ""
    # Already in 'working' so update() takes the reannounce/new-command path,
    # not the transition path.
    api._last_state_for_bubble = "working"
    api._last_working_bubble_at = 0.0
    api._last_working_bubble_text = ""
    return api


def test_new_command_announces_immediately_despite_throttle():
    """A new command appears only 1s into the working state -- well inside
    WORKING_REANNOUNCE_SEC -- and must still fire right away."""
    api = _make_api(claude_cmd=["/usr/local/bin/pytest", "-x"])
    api._last_working_bubble_at = 100.0  # last beat was "just now"

    api.update(PetState(state="working", timestamp=101.0))  # 1s later

    assert api._pending_bubble == "runs pytest"
    assert api._pending_bubble_priority == BUBBLE_PRIO_AMBIENT
    # Clock reset so the next periodic beat lands a full window later.
    assert api._last_working_bubble_at == 101.0


def test_same_command_does_not_reannounce():
    """The identical command on the next tick is deduped -- no chatter spam."""
    api = _make_api(claude_cmd=["pytest", "-x"])
    api._last_working_bubble_text = "runs pytest"
    api._last_working_bubble_at = 100.0

    api.update(PetState(state="working", timestamp=101.0))

    assert api._pending_bubble is None


def test_different_command_reannounces_immediately():
    """A/B/A style: after 'runs pytest', a git push must fire on its own tick
    even though only 1s passed."""
    api = _make_api(claude_cmd=["git", "push", "origin", "main"])
    api._last_working_bubble_text = "runs pytest"
    api._last_working_bubble_at = 100.0

    api.update(PetState(state="working", timestamp=101.0))

    assert api._pending_bubble == "runs git push"


def test_no_live_command_falls_through_to_periodic_beat():
    """No shell child caught (short Bash call already gone): the new-command
    path stays silent and the periodic reannounce owns the beat. Before the
    throttle window it stays quiet."""
    api = _make_api(claude_cmd=None)
    api._last_working_bubble_at = 100.0

    api.update(PetState(state="working", timestamp=101.0))  # 1s in, throttled

    assert api._pending_bubble is None


def test_periodic_beat_still_fires_after_window():
    """With no concrete command, once WORKING_REANNOUNCE_SEC elapses the
    generic 'still working' beat fires (ambient presence)."""
    api = _make_api(claude_cmd=None)
    api._last_working_bubble_at = 100.0

    api.update(PetState(state="working",
                        timestamp=100.0 + WORKING_REANNOUNCE_SEC + 0.1))

    assert api._pending_bubble is not None
    assert api._pending_bubble_priority == BUBBLE_PRIO_AMBIENT


def test_reannounce_cadence_is_15s():
    """Pink-2026-09-13: periodic working updates every 15s, down from 30."""
    assert WORKING_REANNOUNCE_SEC == 15.0
