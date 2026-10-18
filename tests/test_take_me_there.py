"""PetApi.take_me_there(): a double-click raises the window responsible for
whatever she is currently showing.

Pink-2026-09-01: started as "take me to the session that's waving", then
Pink asked for the same on every active sprite -- "except idle/drowsy/
sleeping, because they mean she's doing nothing".

Same __new__ + manual-attribute pattern as test_acknowledge_approval.py,
so no real window is ever raised.
"""
from __future__ import annotations

import json
import threading

import pytest

from squid_pet.watcher import PetState
from squid_pet.window import PetApi


def _api(state: str, focus_fn=None, target=None):
    api = PetApi.__new__(PetApi)
    api._lock = threading.Lock()
    api._latest = PetState(state=state, focus_target=target)
    api._hint_text = ""
    api._hint_seq = 0
    api._focus_inflight_lock = threading.Lock()
    api._focus_inflight_targets = set()
    if focus_fn is not None:
        api._focus_fn = lambda snapshot: focus_fn(snapshot.state)
    return api


# Realistic focus targets, shaped like the ones watcher.compute() builds.
# Project A's shell command (a working owner), project B's Claude approval.
_TARGET_A = {"agent": "claude", "owner": {"pid": 101, "created": 1.0}}
_TARGET_B = {"agent": "claude"}
_GUARD_ATTRS = ("_focus_inflight_lock", "_focus_inflight_targets")


@pytest.mark.parametrize("state", ["working", "thinking", "approval_needed",
                                   "celebrating", "grooving"])
def test_active_states_are_taken_to_their_window(state):
    seen = []
    api = _api(state, focus_fn=lambda s: seen.append(s) or "matched")
    result = api.take_me_there()
    assert result == {"status": "matched", "state": state}
    assert seen == [state], "the state itself picks which signal dir is read"


@pytest.mark.parametrize("state", ["idle", "sleeping"])
def test_resting_states_take_you_nowhere(state):
    api = _api(state, focus_fn=lambda s: "resting")
    assert api.take_me_there()["status"] == "resting"


def test_a_failed_raise_never_breaks_the_gesture():
    """The poke and heart already happened; a broken osascript must not
    surface as an error to the user."""
    def _boom(_state):
        raise RuntimeError("osascript exploded")
    api = _api("working", focus_fn=_boom)
    assert api.take_me_there()["status"] == "error"


def test_inert_without_a_focus_callable():
    """Tests (and any construction path that skips __init__) simply never
    raise a window."""
    api = _api("working")
    assert api.take_me_there()["status"] == "skipped"


def test_a_missing_in_flight_guard_does_not_block_the_raise():
    """Pink-2026-10-12: take_me_there reads _focus_fn defensively because
    PetApi can be built via __new__ (skipping __init__). The in-flight guard
    is read the same way: a PetApi with a focus callable but no guard must
    still raise the window -- an AttributeError here would escape the
    JS-exposed method, and the frontend's .catch would swallow it silently.

    Pink-2026-10-17: the guard is now a lock plus a set of in-flight target
    keys. Missing either one -- or both -- means no guard, never an error.
    The snapshot carries a real target so the guarded path is the one under
    test (a falsy target bypasses the guard before it is ever read)."""
    for missing in (_GUARD_ATTRS, _GUARD_ATTRS[:1], _GUARD_ATTRS[1:]):
        api = _api("working", focus_fn=lambda s: "matched", target=_TARGET_A)
        for attr in missing:
            delattr(api, attr)
        assert api.take_me_there() == {"status": "matched", "state": "working"}, missing


def test_a_failed_raise_without_the_guard_is_still_an_error_status():
    def _boom(_state):
        raise RuntimeError("osascript exploded")
    for missing in (_GUARD_ATTRS, _GUARD_ATTRS[:1], _GUARD_ATTRS[1:]):
        api = _api("working", focus_fn=_boom, target=_TARGET_A)
        for attr in missing:
            delattr(api, attr)
        assert api.take_me_there()["status"] == "error", missing


def test_navigation_receives_the_state_and_its_exact_source_snapshot():
    api = _api('working')
    api._latest.focus_target = {'agent': 'codex', 'owner': {'pid': 123, 'created': 50}}
    seen = []
    api._focus_fn = lambda snapshot: seen.append(snapshot) or 'matched'
    assert api.take_me_there()['status'] == 'matched'
    assert seen == [api._latest]
    assert seen[0].focus_target['owner']['pid'] == 123


def test_no_window_status_emits_a_hint():
    api = _api("concerned", focus_fn=lambda s: "no-window")
    result = api.take_me_there()
    assert result == {"status": "no-window", "state": "concerned"}
    assert api._hint_seq == 1
    assert "no window" in api._hint_text


def test_silent_none_status_emits_no_hint():
    api = _api("concerned", focus_fn=lambda s: "none")
    api.take_me_there()
    assert api._hint_seq == 0


# ─── In-flight guard ─────────────────────────────────────────────────────
# Each window raise costs one osascript process: ~0.6-0.8s of fixed spawn +
# AppleEvent overhead on a real machine, with focus._run_osascript capped at
# 5s. AppleEvents to one app serialize, so a burst of gestures used to queue
# N osascript calls behind each other and blow that timeout (squid.log,
# 2026-09-26 / 09-27 / 10-12: "focus (osascript) failed: ... timed out after
# 5 seconds"). pywebview runs each JS->Python call on its own thread, so the
# calls really do overlap.
#
# Pink-2026-10-17: the drop is per RESOLVED destination -- exactly what
# focus_for_snapshot reads: for a Claude target, the signal directory its
# state resolves to plus the target; for a Codex target, the target alone. A
# call is dropped only when a raise for that same destination is still
# running, because only then is the running raise taking you there. A
# different destination raises; a call with nothing to open (resting, no
# focus_target, a no-window Codex run, a Claude state with no signal
# directory) never touches the guard at all.


def _blocking_focus(calls: list, release: threading.Event,
                    entered: threading.Event, status: str = "matched"):
    def _focus(state):
        calls.append(state)
        entered.set()
        assert release.wait(5), "test never released the in-flight raise"
        return status
    return _focus


def _start_in_flight(api):
    """Run take_me_there on a worker thread, as pywebview does, and hand
    back the thread plus a box that receives its result."""
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(api.take_me_there()))
    t.start()
    return t, box


def _set_snapshot(api, state, target):
    """The watcher's 1 Hz tick publishing a new snapshot mid-raise."""
    with api._lock:
        api._latest = PetState(state=state, focus_target=target)


def test_a_call_while_a_raise_is_in_flight_is_dropped():
    """Same state, same target: the raise already running resolves exactly
    this window, so a second osascript would only queue behind it."""
    calls: list = []
    release, entered = threading.Event(), threading.Event()
    api = _api("working", focus_fn=_blocking_focus(calls, release, entered),
               target=_TARGET_A)
    t, first = _start_in_flight(api)
    try:
        assert entered.wait(5)
        second = api.take_me_there()
        assert second == {"status": "busy", "state": "working"}
        assert calls == ["working"], "the dropped call must not spawn a second osascript"
    finally:
        release.set()
        t.join(5)
    assert first == {"status": "matched", "state": "working"}


def test_a_busy_drop_tells_the_user_a_raise_is_already_running():
    """Pink-2026-10-12: a dropped call used to be silent, so "dropped as busy"
    looked exactly like "did nothing" -- and the natural reaction to that is
    to double-click again, which is the very gesture that gets dropped. The
    drop now says so through the same hint pill no-window uses.

    Pink-2026-10-14: the hint says only what is true -- a raise is already
    running -- and does not promise she will get there: a hung osascript
    times out to status "none", which is deliberately silent
    (test_silent_none_status_emits_no_hint), so a promise would then quietly
    fail."""
    calls: list = []
    release, entered = threading.Event(), threading.Event()
    api = _api("working", focus_fn=_blocking_focus(calls, release, entered),
               target=_TARGET_A)
    t, _ = _start_in_flight(api)
    try:
        assert entered.wait(5)
        assert api._hint_seq == 0, "the raise itself emits no hint"
        # A dict equal to the in-flight target but built separately, with
        # its keys in another order: the key must not depend on identity or
        # on dict ordering.
        _set_snapshot(api, "working",
                      {"owner": {"created": 1.0, "pid": 101}, "agent": "claude"})
        assert api.take_me_there()["status"] == "busy"
        assert api._hint_seq == 1, "exactly one hint per dropped call"
        assert api._hint_text == "still trying to open that window…"
        assert "on my way" not in api._hint_text, "must not promise arrival"
        assert calls == ["working"]
    finally:
        release.set()
        t.join(5)
    assert api._hint_seq == 1, "the in-flight raise finishing adds no hint"


def test_a_different_target_while_a_raise_is_in_flight_still_raises():
    """Pink-2026-10-17: the drop used to be target-blind. Squid is working on
    project A; a dblclick starts A's raise, which can hold for 0.6-5s. A tick
    later she flips to approval_needed for project B and the user
    double-clicks the waving sprite. The frontend acks first, so the wave is
    calmed -- then the old guard dropped B's take_me_there as busy: no raise,
    no wave left to prompt a retry, and a hint naming the wrong window. A
    different destination must raise."""
    calls: list = []
    release, entered = threading.Event(), threading.Event()
    blocking = _blocking_focus(calls, release, entered)
    api = _api("working", target=_TARGET_A)
    api._focus_fn = lambda snap: (blocking(snap.state)
                                  if snap.focus_target == _TARGET_A
                                  else calls.append(snap.state) or "matched")
    t, first = _start_in_flight(api)
    try:
        assert entered.wait(5)
        _set_snapshot(api, "approval_needed", _TARGET_B)
        assert api.take_me_there() == {"status": "matched", "state": "approval_needed"}
        assert calls == ["working", "approval_needed"]
        assert api._hint_seq == 0, api._hint_text
    finally:
        release.set()
        t.join(5)
    assert first == {"status": "matched", "state": "working"}


def test_the_same_target_in_another_state_is_another_destination():
    """Pink-2026-10-17: a Claude target is often just {"agent": "claude"} --
    approval_needed always is -- and focus_for_snapshot resolves it through
    the signal directory OF THE STATE. celebrating and approval_needed map to
    different directories (claude_task_complete/ vs claude_awaiting_input/),
    so equal target dicts there raise different windows and must not drop
    each other. (Contrast working/thinking, which share one directory --
    test_working_then_thinking_with_the_same_target_is_dropped.)"""
    calls: list = []
    release, entered = threading.Event(), threading.Event()
    blocking = _blocking_focus(calls, release, entered)
    api = _api("celebrating", target=_TARGET_B)
    api._focus_fn = lambda snap: (blocking(snap.state)
                                  if snap.state == "celebrating"
                                  else calls.append(snap.state) or "matched")
    t, _ = _start_in_flight(api)
    try:
        assert entered.wait(5)
        _set_snapshot(api, "approval_needed", dict(_TARGET_B))
        assert api.take_me_there() == {"status": "matched", "state": "approval_needed"}
        assert calls == ["celebrating", "approval_needed"]
    finally:
        release.set()
        t.join(5)


def _drop_case(first_state: str, target: dict, second_state: str):
    """Start a blocking raise for (first_state, target), then publish
    second_state with an equal-but-separate target and double-click again.
    The second call answers "matched" without blocking if it is (wrongly)
    let through, so a failure shows up as a status, not a 5s hang."""
    calls: list = []
    release, entered = threading.Event(), threading.Event()
    blocking = _blocking_focus(calls, release, entered)
    api = _api(first_state, target=target)
    api._focus_fn = lambda snap: (blocking(snap.state) if not calls
                                  else calls.append(snap.state) or "matched")
    t, first = _start_in_flight(api)
    try:
        assert entered.wait(5)
        _set_snapshot(api, second_state, json.loads(json.dumps(target)))
        second = api.take_me_there()
    finally:
        release.set()
        t.join(5)
    return api, calls, first, second


def test_working_then_thinking_with_the_same_target_is_dropped():
    """Pink-2026-10-17: the regression the (state, target) key reintroduced.
    focus maps working AND thinking to claude_turn_active/, and the watcher's
    sticky-working and thinking branches both build focus_target from the same
    streaming_target() -- so a routine working -> thinking tick is the SAME
    session, the SAME tab. Keying on state let the second dblclick spawn a
    second osascript at that tab, serialized behind the first AppleEvent: the
    5s timeout pile-up this whole guard exists to stop."""
    api, calls, first, second = _drop_case("working", _TARGET_A, "thinking")
    assert second == {"status": "busy", "state": "thinking"}
    assert calls == ["working"], "the dropped call must not spawn a second osascript"
    assert api._hint_text == "still trying to open that window…"
    assert first == {"status": "matched", "state": "working"}


@pytest.mark.parametrize("second_state",
                         ["thinking", "celebrating", "concerned", "approval_needed"])
def test_an_unchanged_codex_target_across_states_is_dropped(second_state):
    """Pink-2026-10-17: focus_for_snapshot never reads the state for a Codex
    target -- it raises the request's or owner's window straight from the
    target -- so one Codex target is one window whatever she shows. State in
    the key over-split every Codex state change into a second osascript."""
    codex = {"agent": "codex", "owner": {"pid": 202, "created": 2.0}}
    api, calls, first, second = _drop_case("working", codex, second_state)
    assert second == {"status": "busy", "state": second_state}
    assert calls == ["working"]
    assert first == {"status": "matched", "state": "working"}


def _bypass_case(state: str, target: dict):
    """Hold a call for (state, target) open with a blocking stand-in --
    the microseconds the real instant answer takes, stretched -- then make
    the same call through the real resolver. If the first took the guard,
    the second is dropped as busy."""
    from squid_pet import focus
    calls: list = []
    release, entered = threading.Event(), threading.Event()
    blocking = _blocking_focus(calls, release, entered)
    api = _api(state, target=target)
    api._focus_fn = lambda snap: (blocking(snap.state) if not calls
                                  else focus.focus_for_snapshot(snap))
    t, _ = _start_in_flight(api)
    try:
        assert entered.wait(5)
        held = set(api._focus_inflight_targets)
        second = api.take_me_there()
    finally:
        release.set()
        t.join(5)
    return api, second, held


def test_a_no_window_codex_target_bypasses_the_guard():
    """Pink-2026-10-17: {"agent": "codex", "no_window": True} answers
    "no-window" at once without spawning osascript, so it has nothing to
    protect. Taking the guard let it report busy, and the busy hint REPLACED
    the true one -- the run was headless or has exited."""
    api, second, held = _bypass_case("concerned",
                                     {"agent": "codex", "no_window": True})
    assert second == {"status": "no-window", "state": "concerned"}
    assert api._hint_seq == 1, api._hint_text
    assert api._hint_text == (
        "no window to open — that Codex run was headless or has exited")
    assert held == set(), "must not take the guard"


def test_a_claude_target_in_a_state_with_no_signal_dir_bypasses_the_guard():
    """Pink-2026-10-17: a Claude target whose state focus has no signal
    directory for answers at once (focus._focus_claude_state), spawning
    nothing -- so it must not take the guard or report busy either."""
    from squid_pet import focus
    state = "surprised"  # active-looking, but named by no signal dir
    assert state not in focus.STATE_SIGNAL_DIRS
    assert state not in focus.RESTING_STATES
    api, second, held = _bypass_case(state, {"agent": "claude"})
    assert second == {"status": "resting", "state": state}
    assert api._hint_seq == 0, api._hint_text
    assert held == set(), "must not take the guard"


def test_the_key_follows_focus_own_state_to_dir_mapping(monkeypatch):
    """Pink-2026-10-17: the key reads focus.STATE_SIGNAL_DIRS -- the dict
    _focus_claude_state itself resolves from -- rather than a copy, so the
    guard and the resolver can never drift apart."""
    from squid_pet import focus, watcher
    from squid_pet.window import _focus_destination_key as key
    monkeypatch.setitem(focus.STATE_SIGNAL_DIRS, "pondering",
                        watcher.CLAUDE_TURN_ACTIVE_DIR)
    assert key("pondering", _TARGET_A) == key("working", _TARGET_A) is not None
    monkeypatch.delitem(focus.STATE_SIGNAL_DIRS, "grooving")
    assert key("grooving", _TARGET_B) is None


def test_an_unknown_agent_target_is_keyed_on_the_target_alone():
    """Pink-2026-10-17: the watcher builds only claude and codex targets, so
    any other agent is a resolver branch that does not exist yet. Guard it
    the conservative way -- by the target alone, state left out -- so a
    future branch that spawns osascript is still guarded and can never be
    over-split back into the pile-up."""
    from squid_pet.window import _focus_destination_key as key
    other = {"agent": "gemini", "owner": {"pid": 7, "created": 3.0}}
    assert key("working", other) == key("celebrating", other) is not None
    assert key("working", other) != key("working", {"agent": "gemini"})


@pytest.mark.parametrize("state", ["grooving", "concerned", "approval_needed"])
@pytest.mark.parametrize("empty_target", [None, {}])
def test_a_call_with_nothing_to_open_during_an_in_flight_raise_emits_no_hint(
        state, empty_target):
    """Pink-2026-10-17: focus_for_snapshot answers "none" at once, spawning
    nothing, for an active state with no focus_target (grooving with no
    Claude source, a Codex-sourced concern, a cleared Codex request). "none"
    is deliberately silent (test_silent_none_status_emits_no_hint), but the
    guard used to be taken before anything knew that, so such a dblclick
    showed "still trying to open that window..." while an unrelated raise
    ran. It must bypass the guard and return what focus gives."""
    from squid_pet import focus
    calls: list = []
    release, entered = threading.Event(), threading.Event()
    blocking = _blocking_focus(calls, release, entered)
    api = _api("working", target=_TARGET_A)
    # The real resolver for the empty-target call (it returns before any
    # I/O), the blocking stand-in for the raise already running.
    api._focus_fn = lambda snap: (focus.focus_for_snapshot(snap)
                                  if not snap.focus_target
                                  else blocking(snap.state))
    t, first = _start_in_flight(api)
    try:
        assert entered.wait(5)
        _set_snapshot(api, state, empty_target)
        assert api.take_me_there() == {"status": "none", "state": state}
        assert api._hint_seq == 0, api._hint_text
        assert calls == ["working"]
    finally:
        release.set()
        t.join(5)
    assert first == {"status": "matched", "state": "working"}


_RESTING = ["idle", "sleeping", "drowsy", "stretch"]


def test_resting_states_have_one_source_of_truth_in_focus():
    """Pink-2026-10-14: take_me_there's busy guard skips the resting states
    using focus.RESTING_STATES -- the very set focus_for_snapshot answers
    "resting" for -- so the two can never drift apart."""
    from squid_pet import focus
    assert focus.RESTING_STATES == frozenset(_RESTING)
    for state in focus.RESTING_STATES:
        assert focus.focus_for_snapshot(PetState(state=state)) == "resting"


@pytest.mark.parametrize("state", _RESTING)
def test_a_resting_dblclick_during_an_in_flight_raise_emits_no_hint(state):
    """Pink-2026-10-14: a resting state never spawns osascript (focus answers
    "resting" straight away), so the in-flight guard has nothing to protect
    there. Taking it anyway turned a dblclick on a drowsy Squid into "busy"
    plus a hint about navigation she was never going to do -- contradicting
    the spec's "Double-click a sleeping Squid ... just a poke and a heart".
    A resting dblclick must behave exactly as it did before the guard.

    Pink-2026-10-17: the resting snapshot keeps the in-flight raise's very
    target -- a stale focus_target on a resting state must not make it a
    guarded call either."""
    from squid_pet import focus
    calls: list = []
    release, entered = threading.Event(), threading.Event()
    blocking = _blocking_focus(calls, release, entered)
    api = _api("working", target=_TARGET_A)
    # The real resolver for resting states (it short-circuits before any
    # I/O), the blocking stand-in for the active raise already running.
    api._focus_fn = lambda snap: (focus.focus_for_snapshot(snap)
                                  if snap.state in focus.RESTING_STATES
                                  else blocking(snap.state))
    t, first = _start_in_flight(api)
    try:
        assert entered.wait(5)
        _set_snapshot(api, state, _TARGET_A)
        assert api.take_me_there() == {"status": "resting", "state": state}
        assert api._hint_seq == 0, api._hint_text
        assert calls == ["working"]
        assert len(api._focus_inflight_targets) == 1, "resting adds no key"
    finally:
        release.set()
        t.join(5)
    assert first == {"status": "matched", "state": "working"}
    assert api._focus_inflight_targets == set()


def test_a_call_after_the_raise_finishes_goes_through():
    calls: list = []
    release, entered = threading.Event(), threading.Event()
    api = _api("working", focus_fn=_blocking_focus(calls, release, entered),
               target=_TARGET_A)
    t, _ = _start_in_flight(api)
    assert entered.wait(5)
    release.set()
    t.join(5)
    assert api.take_me_there() == {"status": "matched", "state": "working"}
    assert calls == ["working", "working"]


def test_a_failed_raise_releases_the_guard():
    """An exception inside the raise must not leave its key in flight, or
    every later double-click on that same destination would be dropped as
    busy forever."""
    calls: list = []

    def _boom_once(state):
        calls.append(state)
        if len(calls) == 1:
            raise RuntimeError("osascript exploded")
        return "matched"

    api = _api("working", focus_fn=_boom_once, target=_TARGET_A)
    assert api.take_me_there()["status"] == "error"
    assert api._focus_inflight_targets == set()
    assert api.take_me_there()["status"] == "matched"
    assert calls == ["working", "working"]
    assert api._hint_seq == 0, api._hint_text


def test_a_target_json_cannot_encode_still_raises_unguarded():
    """Pink-2026-10-17: the in-flight key is JSON with sorted keys. A target
    it cannot encode (mixed key types break sort_keys) must fall back to an
    unguarded raise: an exception there would escape before the try, the
    frontend's .catch would swallow it, and the raise would never happen."""
    api = _api("working", focus_fn=lambda s: "matched",
               target={"agent": "claude", 1: "odd"})
    assert api.take_me_there() == {"status": "matched", "state": "working"}
    assert api._focus_inflight_targets == set()


def test_the_guard_is_created_by_the_real_constructor(monkeypatch):
    """take_me_there relies on __init__ creating the guard (the helper above
    sets it by hand). Build a real PetApi -- with its two settings reads
    stubbed so nothing touches ~/.squid-pet -- and check the guard is there
    and free."""
    from squid_pet import window
    monkeypatch.setattr(window, "load_corner", lambda: "bottom-right")
    monkeypatch.setattr(window, "load_settings", lambda: {})
    api = PetApi()
    assert api._focus_inflight_targets == set()
    assert api._focus_inflight_lock.acquire(blocking=False)
    api._focus_inflight_lock.release()
