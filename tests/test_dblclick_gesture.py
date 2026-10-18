"""The pet's click gestures in index.html, run under node.

User report (2026-10-12): double-clicking the pet neither took them to the
session nor showed a heart. The heart was sequenced at the END of the dblclick
handler, behind `await take_me_there()`, whose osascript can sit for its full
5s timeout -- so the heart arrived ~5s late and read as "no heart".

There is no JS test harness (see AGENTS.md), so these tests follow the repo's
pattern: slice the real handler source out of index.html between its section
banners, run it under node with stubs, and assert on what it called. The stubs
mirror the real guards that matter here: maybeShowBubble ignores empty text and
repeats of the bubble already showing (latest-wins), exactly like the real one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from squid_pet import watcher

INDEX_HTML = Path(watcher.__file__).parent / "frontend" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _slice(start_marker: str, end_marker: str) -> str:
    html = INDEX_HTML.read_text()
    start = html.index(start_marker)
    end = html.index(end_marker, start)
    return html[start:end]


def _dblclick_source() -> str:
    return _slice("/* ─────── Double-click:", "/* ─────── Esc:")


def _click_source() -> str:
    return _slice("/* ─────── DRAG:", "/* ─────── Right-click:")


# Stubs. `api` is built per scenario from SCENARIO (below). Each RPC records
# its name in `calls` when invoked; `events` is one ordered log of RPCs,
# hearts and bubbles, so tests can check what happened before what.
_PRELUDE = r"""
const calls = [];
const events = [];
const listeners = {};
const hearts = [];
const bubbles = [];
const logs = [];
let _bubbleText = "";

globalThis.window = globalThis;
globalThis.console = { ...console,
  log: (...a) => logs.push(a.join(" ")),
  warn: (...a) => logs.push("WARN " + a.join(" ")),
  error: (...a) => logs.push("ERR " + a.join(" ")) };
const _classes = new Set();
globalThis.document = {
  addEventListener: (type, fn) => { listeners[type] = fn; },
  getElementById: (id) => id === "pet"
    ? { getBoundingClientRect: () => ({ left: 10, top: 20, width: 100, bottom: 120 }) }
    : null,
  body: { classList: { add: (c) => _classes.add(c), remove: (c) => _classes.delete(c) } },
};
let currentState = SCENARIO.state;
let _currentEdge = SCENARIO.edge || "bottom";
function spawnHearts(x, y) { hearts.push([x, y]); events.push("heart"); }
// Mirrors the real guards: no-op on empty text, latest-wins dedupe.
function maybeShowBubble(text) {
  if (!text) return;
  if (text !== _bubbleText) { _bubbleText = text; bubbles.push(text); events.push("bubble"); }
}
const NEVER = new Promise(() => {});
// Modes: "never" hangs, "reject" rejects (a Python exception crossing the
// pywebview bridge), "throw" throws synchronously, "slow" resolves after a
// real delay and logs "<name>:resolved" so tests can see what started before
// it finished. Anything else is the resolved value.
function rpc(name, value) {
  return (...args) => {
    calls.push(name); events.push(name);
    const v = SCENARIO.rpc[name];
    if (v === "never") return NEVER;
    if (v === "reject") return Promise.reject(new Error(name + " rejected"));
    if (v === "throw") throw new Error(name + " threw");
    if (v === "slow") return new Promise((r) => setTimeout(() => {
      events.push(name + ":resolved"); r(value); }, 20));
    return Promise.resolve(v === undefined ? value : v);
  };
}
// debug_log is an RPC too, so it can fail the same ways; SCENARIO.debug_log
// picks how. Every message it was handed is recorded in debugLogs.
const debugLogs = [];
function debug_log(msg) {
  debugLogs.push(String(msg));
  const v = SCENARIO.debug_log;
  if (v === "throw") throw new Error("debug_log threw");
  if (v === "reject") return Promise.reject(new Error("debug_log rejected"));
  return Promise.resolve();
}
window.pywebview = { api: {
  poke: rpc("poke", "ok"),
  acknowledge_approval: rpc("acknowledge_approval", { status: "calmed", bubble: "okay okay, I see you" }),
  acknowledge_concern: rpc("acknowledge_concern", { status: "calmed", bubble: "phew" }),
  take_me_there: rpc("take_me_there", { status: "matched", state: SCENARIO.state }),
  debug_log,
  drag_start: () => {},
  drag_end: () => {},
} };
const unhandled = [];
process.on("unhandledRejection", (r) => unhandled.push(String(r)));
const settle = () => new Promise((r) => setTimeout(r, 0));
const report = () => process.stdout.write(JSON.stringify(
  { calls, events, hearts, bubbles, logs, debugLogs, unhandled }));
"""


def _run(handler_src: str, scenario: dict, driver: str) -> dict:
    script = (
        f"const SCENARIO = {json.dumps(scenario)};\n"
        + _PRELUDE
        + handler_src
        + "\n(async () => {\n" + driver + "\nreport();\n})();\n"
    )
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                         timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_DBLCLICK = """
listeners.dblclick({ preventDefault() {} });
for (let i = 0; i < 5; i++) await settle();
await new Promise((r) => setTimeout(r, 80));
for (let i = 0; i < 5; i++) await settle();
"""


def _dblclick(state: str, debug_log: str | None = None, **rpc) -> dict:
    scenario: dict = {"state": state, "rpc": rpc}
    if debug_log is not None:
        scenario["debug_log"] = debug_log
    return _run(_dblclick_source(), scenario, _DBLCLICK)


def _assert_failure_logged(r: dict, call: str) -> None:
    """FIX 5 (Pink-2026-10-14): a swallowed RPC failure still leaves a trace,
    in the console AND squid.log (via debug_log), naming the call that
    failed. This bug was diagnosed from squid.log."""
    needle = f"dblclick {call} failed"
    assert any(line.startswith("WARN") and needle in line
               for line in r["logs"]), r["logs"]
    assert any(needle in m for m in r["debugLogs"]), r["debugLogs"]


def test_heart_spawns_even_when_take_me_there_never_resolves():
    """The reported bug: a stalled window raise must not hold back the heart."""
    r = _dblclick("working", take_me_there="never")
    assert r["hearts"] == [[60, 20]], r
    assert "take_me_there" in r["calls"], "the window raise is still requested"


def test_heart_spawns_before_any_rpc():
    """Hearts are a local animation -- they come first, ahead of every RPC,
    so no RPC latency can delay the gesture's feedback."""
    r = _dblclick("working")
    assert r["hearts"] == [[60, 20]]
    assert r["events"][0] == "heart", r["events"]
    assert r["calls"][0] == "poke", "poke is still the first RPC requested"


_ACKS = [("approval_needed", "acknowledge_approval", "okay okay, I see you"),
         ("concerned", "acknowledge_concern", "phew")]


@pytest.mark.parametrize("failure", ["reject", "throw"])
@pytest.mark.parametrize("state, ack, bubble", _ACKS)
def test_a_failed_poke_does_not_gate_the_rest_of_the_gesture(
        failure, state, ack, bubble):
    """Pink-2026-10-12: poke used to be awaited inside the handler's one big
    try, so a poke that failed jumped to the catch and skipped the ack and the
    window raise -- with the heart shown first, the user saw the gesture
    register and then got no ack and no window.

    Pink-2026-10-14: poke is awaited again (see the ordering test below) but
    in its own try, so a failure is logged and the gesture carries on."""
    r = _dblclick(state, poke=failure)
    assert r["hearts"] == [[60, 20]]
    assert r["calls"][0] == "poke"
    assert ack in r["calls"], r["calls"]
    assert r["bubbles"] == [bubble]
    assert "take_me_there" in r["calls"], r["calls"]
    assert r["unhandled"] == []
    assert not any("handler ERROR" in line for line in r["logs"]), r["logs"]
    _assert_failure_logged(r, "poke")


@pytest.mark.parametrize("failure", ["reject", "throw"])
def test_a_failed_poke_still_takes_you_there(failure):
    r = _dblclick("working", poke=failure)
    assert r["hearts"] == [[60, 20]]
    assert "take_me_there" in r["calls"], r["calls"]
    assert "[squid] take_me_there(working) -> matched" in r["logs"]
    assert r["unhandled"] == []


@pytest.mark.parametrize("state", ["working", "approval_needed", "concerned"])
def test_a_hung_poke_still_shows_the_heart_and_fails_quietly(state):
    """Pink-2026-10-14: poke is awaited (the ack ordering depends on it), so
    a poke that NEVER settles does hold back the rest of the gesture -- that
    is the accepted cost of the ordering guarantee, not something this test
    pins either way. What is guaranteed: the heart is already up (it comes
    before any RPC), poke was requested, and nothing escapes as an unhandled
    rejection or a handler ERROR."""
    r = _dblclick(state, poke="never")
    assert r["hearts"] == [[60, 20]]
    assert r["events"][0] == "heart", r["events"]
    assert r["calls"][0] == "poke"
    assert r["unhandled"] == []
    assert not any("handler ERROR" in line for line in r["logs"]), r["logs"]


@pytest.mark.parametrize("state, ack, bubble", _ACKS)
def test_poke_finishes_before_the_ack_starts(state, ack, bubble):
    """Pink-2026-10-14: poke and the ack both write pending_bubble at the
    same priority, and an equal priority overwrites. Awaiting poke makes its
    write land first, so the ack's like-line is the one the next poll sees
    (and dedupes, since the handler already showed it). Fired detached, poke
    could land last and the next poll would replace the ack bubble with the
    generic poke line -- the "ack silently swallowed" failure."""
    r = _dblclick(state, poke="slow")
    ev = r["events"]
    assert ev.index("poke:resolved") < ev.index(ack), ev
    assert r["bubbles"] == [bubble]


_ACK_FAILURES = [(state, ack) for state, ack, _ in _ACKS]


@pytest.mark.parametrize("failure", ["never", "reject", "throw"])
@pytest.mark.parametrize("state, ack", _ACK_FAILURES)
def test_a_failed_ack_does_not_skip_take_me_there(failure, state, ack):
    """Pink-2026-10-14: the acks were awaited inside the handler's one outer
    try, so an ack that rejected (any Python exception crosses the pywebview
    bridge as a rejection) jumped to the outer catch and take_me_there was
    never called -- "it didn't take me there" on the two highest-traffic
    paths. Each ack now has its own try.

    "never" is scoped down, like the hung-poke test: the ack stays awaited
    (its bubble comes back on the response), so an ack that never settles
    does hold back the raise. Guaranteed there: the heart is up, the ack was
    requested, and nothing escapes as an unhandled rejection or ERROR."""
    r = _dblclick(state, **{ack: failure})
    assert r["hearts"] == [[60, 20]]
    assert ack in r["calls"], r["calls"]
    assert r["unhandled"] == []
    assert not any("handler ERROR" in line for line in r["logs"]), r["logs"]
    if failure != "never":
        assert "take_me_there" in r["calls"], r["calls"]
        assert f"[squid] take_me_there({state}) -> matched" in r["logs"]
        _assert_failure_logged(r, ack)


@pytest.mark.parametrize("failure", ["reject", "throw"])
def test_a_failed_take_me_there_is_logged(failure):
    r = _dblclick("working", take_me_there=failure)
    _assert_failure_logged(r, "take_me_there")
    assert r["unhandled"] == []


@pytest.mark.parametrize("log_failure", ["reject", "throw"])
@pytest.mark.parametrize("failing_call", ["poke", "take_me_there"])
def test_logging_a_failure_can_never_break_the_gesture(log_failure, failing_call):
    """Pink-2026-10-14: debug_log is itself an RPC. If it throws or rejects
    while reporting another failure, that must not escape either."""
    r = _dblclick("approval_needed", debug_log=log_failure,
                  **{failing_call: "reject"})
    assert r["hearts"] == [[60, 20]]
    assert "acknowledge_approval" in r["calls"]
    assert "take_me_there" in r["calls"]
    assert r["unhandled"] == []
    assert not any("handler ERROR" in line for line in r["logs"]), r["logs"]


def test_heart_anchors_below_the_sprite_on_the_top_edge():
    """Positioning is unchanged: on the top edge the heart hangs off the
    sprite's bottom, everywhere else it sits on its top."""
    r = _run(_dblclick_source(), {"state": "working", "edge": "top", "rpc": {}},
             _DBLCLICK)
    assert r["hearts"] == [[60, 120]]


def test_take_me_there_result_is_logged_and_resting_is_not():
    r = _dblclick("working")
    assert "[squid] take_me_there(working) -> matched" in r["logs"]
    r = _dblclick("idle", take_me_there={"status": "resting", "state": "idle"})
    assert not any("take_me_there(" in line for line in r["logs"]), r["logs"]
    assert r["hearts"], "a resting dblclick is still a heart"


@pytest.mark.parametrize("failure", ["reject", "throw"])
def test_a_failed_take_me_there_is_swallowed(failure):
    """No unhandled rejection, no handler error, and the heart still shows."""
    r = _dblclick("working", take_me_there=failure)
    assert r["unhandled"] == []
    assert not any("handler ERROR" in line for line in r["logs"]), r["logs"]
    assert r["hearts"]


def test_approval_needed_dblclick_acknowledges_and_shows_its_bubble():
    r = _dblclick("approval_needed", take_me_there="never")
    assert "acknowledge_approval" in r["calls"]
    assert r["bubbles"] == ["okay okay, I see you"]
    assert r["hearts"]
    # The ack still runs before the window raise is requested (the raise
    # navigates off the snapshot; the ack must not be skipped by it).
    assert r["calls"].index("acknowledge_approval") < r["calls"].index("take_me_there")


def test_concerned_dblclick_acknowledges_and_shows_its_bubble():
    r = _dblclick("concerned", take_me_there="never")
    assert "acknowledge_concern" in r["calls"]
    assert "acknowledge_approval" not in r["calls"]
    assert r["bubbles"] == ["phew"]
    assert r["hearts"]


def test_plain_dblclick_does_not_acknowledge():
    r = _dblclick("working")
    assert "acknowledge_approval" not in r["calls"]
    assert "acknowledge_concern" not in r["calls"]
    assert r["bubbles"] == []


def test_single_click_wakes_without_a_heart():
    """Single click = wake only (no heart); the heart is the dblclick reward."""
    driver = """
const ev = { button: 0, screenX: 5, screenY: 5 };
listeners.mousedown(ev);
listeners.mouseup(ev);
await new Promise((r) => setTimeout(r, 400));
"""
    r = _run(_click_source(), {"state": "working", "rpc": {}}, driver)
    assert r["calls"] == ["poke"]
    assert r["hearts"] == []
