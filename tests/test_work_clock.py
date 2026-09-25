"""Regression tests for the accumulated Claude/Codex work clock."""
from __future__ import annotations

import pytest

from squid_pet import watcher


class FakeAgent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.enabled = True
        self.active = False
        self.shell_active = False
        self.file_active = False
        self.streaming = False
        self.shell_owner = None
        self.transcript_age = float("inf")
        self.transcript_path = None
        self.codex_running = False
        self.claude_code_running = False

    def is_busy(self, now: float) -> bool:
        del now
        if self.name == "codex":
            self.codex_running = self.active
        else:
            self.claude_code_running = self.active
        return self.active

    def is_celebrating(self, now: float) -> bool:
        del now
        return False


@pytest.fixture
def isolated_clock(monkeypatch, tmp_path):
    """Keep every external activity/approval signal deterministic.

    Hermeticity (2026-09-24): several signals StateMachine.compute() reads
    directly are NOT function-stubbed above -- they read flag directories under
    the developer's real ~/.squid-pet by way of module-level path constants
    (claude_finished_freshest_age -> CLAUDE_FINISHED_DIR was the culprit: a live
    "just finished" flag on this machine flipped these tests to 'grooving').
    Point every such directory at an empty tmp path so the tests never read --
    or, via sweep_stale_session_ttys, WRITE/DELETE -- real ~/.squid-pet state.
    force_state (read from Path.home() directly, not a constant) is neutralised
    with a no-op so a debug override left on disk can't hijack the cascade.
    """
    clock = {"now": 100.0}
    monkeypatch.setattr(watcher.time, "time", lambda: clock["now"])
    monkeypatch.setattr(watcher, "macos_idle_seconds", lambda: 0.0)
    monkeypatch.setattr(watcher, "claude_turn_in_flight", lambda now=None: False)
    monkeypatch.setattr(watcher, "claude_sessions_awaiting_input", lambda: [])
    monkeypatch.setattr(watcher, "filter_eligible_claude_sessions", lambda ids: ids)
    monkeypatch.setattr(watcher, "claude_sessions_recapping", lambda: [])
    monkeypatch.setattr(watcher, "claude_task_marked_complete_recently", lambda now=None: False)
    monkeypatch.setattr(watcher, "codex_requests_awaiting_input", lambda: [])
    monkeypatch.setattr(watcher, "filter_eligible_codex_requests", lambda ids: ids)
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [])
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path)
    # Isolate every ~/.squid-pet flag dir the cascade + overrides read directly.
    empty = str(tmp_path / "nonexistent-squid-pet")
    for const in (
        "CLAUDE_FINISHED_DIR", "CLAUDE_FAILED_DIR", "CLAUDE_RECAPPING_DIR",
        "CLAUDE_TASK_COMPLETE_DIR", "CLAUDE_TURN_ACTIVE_DIR",
        "CLAUDE_AWAITING_INPUT_DIR", "CLAUDE_SESSION_TTY_DIR",
        "CODEX_AWAITING_INPUT_DIR",
    ):
        monkeypatch.setattr(watcher, const, empty)
    monkeypatch.setattr(
        watcher.StateMachine, "_apply_force_state_override",
        lambda self, st: None)
    return clock


def _make_machine(name: str) -> tuple[watcher.StateMachine, FakeAgent]:
    agent = FakeAgent(name)
    return watcher.StateMachine([agent]), agent


@pytest.mark.parametrize("name", ["claude_code", "codex"])
def test_thinking_and_tool_time_accumulate_for_both_agents(
    name, isolated_clock
):
    clock = isolated_clock
    machine, agent = _make_machine(name)

    agent.active = True
    agent.shell_active = True
    clock["now"] = 100.0
    first = machine.compute(notify=False)
    assert first.state == "working"
    assert first.work_seconds == 0.0

    # A tool-free reasoning/transcript interval is still agent work.
    agent.shell_active = False
    agent.streaming = True
    clock["now"] = 160.0
    thinking = machine.compute(notify=False)
    assert thinking.state == "thinking"
    assert thinking.work_seconds == 60.0

    clock["now"] = 200.0
    resumed = machine.compute(notify=False)
    assert resumed.work_seconds == 100.0


@pytest.mark.parametrize("name", ["claude_code", "codex"])
def test_approval_pause_preserves_and_then_resumes_work_clock(
    name, isolated_clock, monkeypatch
):
    clock = isolated_clock
    machine, agent = _make_machine(name)

    agent.active = True
    agent.shell_active = True
    clock["now"] = 100.0
    machine.compute(notify=False)
    clock["now"] = 160.0
    before_approval = machine.compute(notify=False)
    assert before_approval.work_seconds == 60.0

    # The agent remains alive while its permission request is pending, but
    # the approval interval itself must not be charged as work.
    agent.shell_active = False
    waits = ["request-1"]
    if name == "codex":
        monkeypatch.setattr(watcher, "codex_requests_awaiting_input", lambda: waits)
    else:
        monkeypatch.setattr(watcher, "claude_sessions_awaiting_input", lambda: waits)
    clock["now"] = 260.0
    waiting = machine.compute(notify=False)
    assert waiting.state == "approval_needed"
    assert waiting.work_seconds == 160.0

    clock["now"] = 360.0
    still_waiting = machine.compute(notify=False)
    assert still_waiting.state == "approval_needed"
    assert still_waiting.work_seconds == 160.0

    waits.clear()
    agent.shell_active = True
    clock["now"] = 400.0
    resumed = machine.compute(notify=False)
    assert resumed.state == "working"
    assert resumed.work_seconds == 160.0

    # The first resumed sample establishes a fresh baseline; subsequent
    # samples charge only time observed after the approval ended.
    clock["now"] = 401.0
    after_resume = machine.compute(notify=False)
    assert after_resume.work_seconds == 161.0

    # Once the agent session really ends, a future session starts at zero.
    agent.active = False
    agent.shell_active = False
    clock["now"] = 401.0
    finished = machine.compute(notify=False)
    assert finished.state == "idle"
    assert finished.work_seconds == 0.0


def test_one_agent_waiting_does_not_pause_another_agent(
    isolated_clock, monkeypatch
):
    """Concurrent sessions charge the agent that is still doing work."""
    clock = isolated_clock
    claude = FakeAgent("claude_code")
    codex = FakeAgent("codex")
    machine = watcher.StateMachine([claude, codex])

    claude.active = codex.active = True
    claude.shell_active = codex.shell_active = True
    clock["now"] = 100.0
    machine.compute(notify=False)
    clock["now"] = 160.0
    before_approval = machine.compute(notify=False)
    assert before_approval.work_seconds == 60.0

    codex.shell_active = False
    waits = ["request-1"]
    monkeypatch.setattr(watcher, "codex_requests_awaiting_input", lambda: waits)
    clock["now"] = 260.0
    concurrent = machine.compute(notify=False)
    assert concurrent.state == "approval_needed"
    assert concurrent.work_seconds == 160.0


@pytest.mark.parametrize("name", ["claude_code", "codex"])
def test_pending_approval_still_pauses_after_notification_is_snoozed(
    name, isolated_clock, monkeypatch
):
    clock = isolated_clock
    machine, agent = _make_machine(name)
    agent.active = True
    agent.shell_active = True
    clock["now"] = 100.0
    machine.compute(notify=False)
    clock["now"] = 160.0
    assert machine.compute(notify=False).work_seconds == 60.0

    waits = ["request-1"]
    if name == "codex":
        monkeypatch.setattr(watcher, "codex_requests_awaiting_input", lambda: waits)
        monkeypatch.setattr(watcher, "filter_eligible_codex_requests", lambda ids: [])
    else:
        monkeypatch.setattr(watcher, "claude_sessions_awaiting_input", lambda: waits)
        monkeypatch.setattr(watcher, "filter_eligible_claude_sessions", lambda ids: [])
    agent.shell_active = False
    clock["now"] = 260.0
    snoozed = machine.compute(notify=False)
    assert snoozed.state != "approval_needed"
    assert snoozed.work_seconds == 160.0
    clock["now"] = 360.0
    assert machine.compute(notify=False).work_seconds == 160.0


def test_valid_claude_turn_marker_counts_as_thinking_work(isolated_clock, monkeypatch):
    clock = isolated_clock
    machine, agent = _make_machine("claude_code")
    agent.active = True
    agent.transcript_age = 1.0
    monkeypatch.setattr(watcher, "claude_turn_in_flight", lambda now=None: True)

    clock["now"] = 100.0
    first = machine.compute(notify=False)
    assert first.state == "thinking"
    clock["now"] = 160.0
    second = machine.compute(notify=False)
    assert second.state == "thinking"
    assert second.work_seconds == 60.0


@pytest.mark.parametrize("active,enabled", [(True, True), (False, True), (True, False)])
def test_stalled_or_exited_claude_turn_marker_does_not_count_as_work(
    active, enabled, isolated_clock, monkeypatch
):
    clock = isolated_clock
    machine, agent = _make_machine("claude_code")
    agent.active = active
    agent.enabled = enabled
    agent.transcript_age = 9999.0
    monkeypatch.setattr(watcher, "claude_turn_in_flight", lambda now=None: True)

    clock["now"] = 100.0
    first = machine.compute(notify=False)
    assert first.state == "idle"
    clock["now"] = 160.0
    second = machine.compute(notify=False)
    assert second.state == "idle"
    assert second.work_seconds == 0.0


def test_active_codex_turn_counts_as_thinking_work(isolated_clock, monkeypatch):
    clock = isolated_clock
    machine, agent = _make_machine("codex")
    agent.active = True
    monkeypatch.setattr(
        "squid_pet.codex_turns.active_turns",
        lambda now=None: [{"pid": 7, "created": 1.0, "updated": 100.0, "key": "turn"}],
    )

    clock["now"] = 100.0
    first = machine.compute(notify=False)
    assert first.state == "thinking"
    clock["now"] = 160.0
    second = machine.compute(notify=False)
    assert second.state == "thinking"
    assert second.work_seconds == 60.0
