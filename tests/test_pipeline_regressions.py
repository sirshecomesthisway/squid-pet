"""Reproductions from the detector audit, using real cascade and temporary signals."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from squid_pet import config, detectors, watcher
from squid_pet.detectors import ClaudeCodeDetector, CodexDetector, GitDetector, IDEDetector


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = [10_000.0]
    monkeypatch.setattr(watcher.time, "time", lambda: clock[0])
    monkeypatch.setattr(watcher, "macos_idle_seconds", lambda: 0.0)
    for name in (
        "CLAUDE_AWAITING_INPUT_DIR", "CODEX_AWAITING_INPUT_DIR", "CLAUDE_FINISHED_DIR",
        "CLAUDE_RECAPPING_DIR", "CLAUDE_TASK_COMPLETE_DIR", "CLAUDE_TURN_ACTIVE_DIR",
    ):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setattr(watcher, name, str(directory))
    monkeypatch.setattr(watcher, "_CLAUDE_SESSION_FLAG_FIRST_SEEN", {})
    monkeypatch.setattr(watcher, "_CODEX_SESSION_FLAG_FIRST_SEEN", {})
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [])
    monkeypatch.setattr(watcher.StateMachine, "_apply_force_state_override", lambda *_: None)
    monkeypatch.setattr(config, "get", lambda key, default=None: default)
    monkeypatch.setattr(detectors, "_tree_walk_cache", None)
    return clock


def flag(directory, name, timestamp):
    path = Path(directory) / name
    path.write_text("signal")
    os.utime(path, (timestamp, timestamp))
    return path


def agent(kind, *, running=True, shell=False, age=float("inf"), now=10_000.0):
    cls = ClaudeCodeDetector if kind == "claude" else CodexDetector
    return cls(
        find_processes_fn=lambda: [object()] if running else [],
        aggregate_cpu_fn=lambda _: 0.0,
        has_active_shell_children_fn=lambda _: shell,
        shell_cmdline_fn=lambda _: ["pytest"] if shell else None,
        recent_file_ages_fn=lambda: [],
        glob_fn=lambda _: [Path("/nonexistent/session.jsonl")] if age != float("inf") else [],
        stat_fn=lambda _: SimpleNamespace(st_mtime=now - age),
    )


@pytest.mark.parametrize("owner,active_kind", [
    ("claude", "codex"), ("claude", "claude"), ("codex", "codex"),
])
def test_other_activity_cannot_delete_a_permission_wait(world, monkeypatch, owner, active_kind):
    directory = (watcher.CLAUDE_AWAITING_INPUT_DIR if owner == "claude"
                 else watcher.CODEX_AWAITING_INPUT_DIR)
    marker = flag(directory, "waiting-session", world[0] - 4)
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [object()])
    sm = watcher.StateMachine(detectors=[agent(active_kind, shell=True)])
    assert sm.compute(notify=False).state == "approval_needed"
    assert marker.exists()


def test_transcript_residue_after_permission_prompt_cannot_clear_it(world, monkeypatch):
    marker = flag(watcher.CLAUDE_AWAITING_INPUT_DIR, "s", world[0] - 4)
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [object()])
    sm = watcher.StateMachine(detectors=[agent("claude", age=5)])
    assert sm.compute(notify=False).state == "approval_needed"
    assert marker.exists()


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_exited_agent_transcript_cannot_borrow_other_agents_process(world, kind):
    other = "codex" if kind == "claude" else "claude"
    sm = watcher.StateMachine(detectors=[agent(kind, running=False, age=1), agent(other)])
    assert sm.compute(notify=False).state == "idle"


@pytest.mark.parametrize("signal,want", [
    ("edit", "thinking"), ("burst", "grooving"), ("git", "celebrating"),
    ("turn", "thinking"), ("recap", "thinking"), ("stop", "grooving"),
])
def test_each_fresh_signal_wakes_a_sleeping_machine(world, tmp_path, signal, want):
    inputs = []
    if signal in {"edit", "burst"}:
        inputs.append(IDEDetector(process_iter_fn=lambda: [],
                                  recent_files_fn=lambda _: [1] if signal == "edit" else [8] * 5))
    elif signal == "git":
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True)
        flag(root / ".git", "HEAD", world[0])
        inputs.append(GitDetector(project_dirs=[str(root)]))
    else:
        inputs.append(agent("claude", age=60))
        directory = {"turn": watcher.CLAUDE_TURN_ACTIVE_DIR,
                     "recap": watcher.CLAUDE_RECAPPING_DIR,
                     "stop": watcher.CLAUDE_FINISHED_DIR}[signal]
        flag(directory, "s", world[0])
    sm = watcher.StateMachine(detectors=inputs)
    sm._agent_idle_since = world[0] - 315
    assert sm.compute(notify=False).state == want


def test_codex_work_outweighs_another_sessions_stop(world):
    flag(watcher.CLAUDE_FINISHED_DIR, "finished-claude", world[0])
    sm = watcher.StateMachine(detectors=[agent("claude"), agent("codex", shell=True)])
    assert sm.compute(notify=False).state == "working"


def test_new_turn_outweighs_previous_stop(world):
    flag(watcher.CLAUDE_FINISHED_DIR, "s", world[0] - 1)
    flag(watcher.CLAUDE_TURN_ACTIVE_DIR, "s", world[0])
    sm = watcher.StateMachine(detectors=[agent("claude", age=1)])
    assert sm.compute(notify=False).state == "thinking"


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_future_transcript_is_not_current_activity(world, kind):
    assert agent(kind, age=-3600).is_busy(world[0]) is False


def test_future_project_file_is_not_current_activity(world, tmp_path):
    flag(tmp_path, "future.py", world[0] + 3600)
    assert detectors._scan_recent_file_ages([tmp_path], 30, now=world[0]) == []


def test_future_or_directory_flag_is_not_a_wait(world):
    future = flag(watcher.CLAUDE_AWAITING_INPUT_DIR, "future", world[0] + 3600)
    (Path(watcher.CLAUDE_AWAITING_INPUT_DIR) / "not-a-signal").mkdir()
    assert watcher.claude_sessions_awaiting_input() == []
    assert future.exists()  # clock skew is not authority to destroy a request


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_snoozed_marker_replaced_between_ticks_rearms(world, kind):
    directory = (watcher.CLAUDE_AWAITING_INPUT_DIR if kind == "claude"
                 else watcher.CODEX_AWAITING_INPUT_DIR)
    marker = flag(directory, "same-request", world[0])
    sm = watcher.StateMachine(detectors=[])
    assert sm.compute(notify=False).state == "approval_needed"
    watcher.snooze_all_awaiting_now()
    assert sm.compute(notify=False).state == "idle"
    world[0] += 1
    marker.unlink()
    flag(directory, marker.name, world[0])
    assert sm.compute(notify=False).state == "approval_needed"


def test_failed_process_tree_does_not_hide_later_sessions_tool(world):
    def broken(**_):
        raise SystemError("process vanished in C layer")
    bad = SimpleNamespace(children=broken)
    child = SimpleNamespace(name=lambda: "pytest", cmdline=lambda: ["pytest"])
    good = SimpleNamespace(children=lambda **_: [child])
    assert watcher.shell_child_activity([bad, good]) == (True, ["pytest"])


def test_child_that_exits_during_probe_does_not_count_as_work(world):
    def vanished():
        raise psutil.NoSuchProcess(123)
    child = SimpleNamespace(name=lambda: "pytest", cmdline=vanished)
    parent = SimpleNamespace(children=lambda **_: [child])
    assert watcher.shell_child_activity([parent]) == (False, None)


@pytest.mark.parametrize("payload", [[], "bad", {"triggers": []}, {"triggers": None}])
def test_malformed_settings_do_not_break_machine(world, tmp_path, monkeypatch, payload):
    import json
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(payload))
    monkeypatch.setattr(watcher.StateMachine, "_SETTINGS_FILE", settings)
    sm = watcher.StateMachine()
    assert {d.name for d in sm.detectors} == {"claude_code", "codex", "git", "terminal", "ide"}


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_new_process_invalidates_transcript_discovery_cache(world, tmp_path, kind):
    paths = []
    procs = [SimpleNamespace(pid=1)]
    old = flag(tmp_path, "old.jsonl", world[0] - 100)
    paths.append(old)
    cls = ClaudeCodeDetector if kind == "claude" else CodexDetector
    d = cls(find_processes_fn=lambda: procs, aggregate_cpu_fn=lambda _: 0,
            has_active_shell_children_fn=lambda _: False, recent_file_ages_fn=lambda: [],
            glob_fn=lambda _: iter(paths))
    assert not d.is_busy(world[0])
    world[0] += 1
    procs.append(SimpleNamespace(pid=2))
    paths.append(flag(tmp_path, "new.jsonl", world[0]))
    assert d.is_busy(world[0])


@pytest.mark.parametrize("kind", ["claude", "codex"])
@pytest.mark.parametrize("age,want", [(19.999, True), (20.0, False), (20.001, False)])
def test_transcript_freshness_boundary(world, kind, age, want):
    assert agent(kind, age=age).is_busy(world[0]) is want


@pytest.mark.parametrize("age,want", [(179.999, "thinking"), (180.0, "thinking"),
                                      (180.001, "idle")])
def test_open_turn_stall_boundary(world, age, want):
    flag(watcher.CLAUDE_TURN_ACTIVE_DIR, "s", world[0] - 200)
    sm = watcher.StateMachine(detectors=[agent("claude", age=age)])
    assert sm.compute(notify=False).state == want


@pytest.mark.parametrize("age,want", [(19.999, True), (20.0, True), (20.001, False)])
def test_completion_marker_boundary(world, age, want):
    flag(watcher.CLAUDE_TASK_COMPLETE_DIR, "s", world[0] - age)
    assert watcher.claude_task_marked_complete_recently(world[0]) is want


@pytest.mark.parametrize("elapsed,want", [(120.0, "approval_needed"), (120.001, "idle")])
def test_wait_snooze_boundary(world, elapsed, want):
    flag(watcher.CLAUDE_AWAITING_INPUT_DIR, "s", world[0])
    sm = watcher.StateMachine(detectors=[])
    assert sm.compute(notify=False).state == "approval_needed"
    world[0] += elapsed
    assert sm.compute(notify=False).state == want


def test_zombie_child_cannot_keep_agent_working(world):
    child = SimpleNamespace(name=lambda: "pytest", cmdline=lambda: ["pytest"],
                            status=lambda: psutil.STATUS_ZOMBIE)
    parent = SimpleNamespace(children=lambda **_: [child])
    assert watcher.shell_child_activity([parent]) == (False, None)


def test_versioned_python_child_is_active_tool(world):
    child = SimpleNamespace(name=lambda: "python3.13", cmdline=lambda: ["python3.13", "job.py"])
    parent = SimpleNamespace(children=lambda **_: [child])
    assert watcher.shell_child_activity([parent]) == (True, ["python3.13", "job.py"])


def test_git_activity_bubble_does_not_claim_a_commit(world, tmp_path):
    from squid_pet.observer import Observer
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    # HEAD mtime changes on checkout too; there is no commit evidence here.
    flag(root / ".git", "HEAD", world[0])
    sm = watcher.StateMachine(detectors=[GitDetector(project_dirs=[str(root)])])
    state = sm.compute(notify=False)
    line = Observer(get_muted=lambda: False).on_state_change(
        "idle", state.state, state_reason=state.state_reason)
    assert state.state == "celebrating"
    assert line == "git activity!"


def test_codex_async_hook_wait_survives_other_work_then_reply_clears(world, tmp_path, monkeypatch):
    import runpy
    hook = runpy.run_path(str(Path(__file__).parents[1] / "scripts/codex_pet_hook.py"))["handle"]
    monkeypatch.setattr(watcher, "CODEX_AWAITING_INPUT_DIR", str(tmp_path / "codex_awaiting_input"))
    payload = dict(session_id="s", turn_id="t", tool_name="request_user_input_async",
                   tool_use_id="question", tool_response={"accepted": True})
    hook({**payload, "hook_event_name": "PostToolUse"}, tmp_path)
    for path in (tmp_path / "codex_awaiting_input").glob("[!.]*"):
        os.utime(path, (world[0] - 10, world[0] - 10))
    sm = watcher.StateMachine(detectors=[agent("codex", shell=True)])
    assert sm.compute(notify=False).state == "approval_needed"
    hook({"session_id": "s", "turn_id": "t", "hook_event_name": "Stop"}, tmp_path)
    assert sm.compute(notify=False).state == "approval_needed"
    hook({"session_id": "s", "hook_event_name": "UserPromptSubmit"}, tmp_path)
    assert sm.compute(notify=False).state == "working"


@pytest.mark.parametrize("name", ["HEAD", "index", "refs/heads"])
def test_future_git_metadata_does_not_signal_activity(world, tmp_path, name):
    root = tmp_path / "repo"
    gitdir = root / ".git"
    gitdir.mkdir(parents=True)
    if name == "refs/heads":
        path = gitdir / name
        path.mkdir(parents=True)
        os.utime(path, (world[0] + 3600, world[0] + 3600))
    else:
        flag(gitdir, name, world[0] + 3600)
    d = GitDetector(project_dirs=[str(root)])
    assert not d.is_busy(world[0])
    assert not d.is_celebrating(world[0])


@pytest.mark.parametrize("elapsed,want", [(314.999, "idle"), (315.0, "sleeping")])
def test_sleep_boundary_with_no_signals(world, elapsed, want):
    sm = watcher.StateMachine(detectors=[])
    sm._agent_idle_since = world[0] - elapsed
    assert sm.compute(notify=False).state == want


def test_working_hold_expires_at_deadline_while_streaming_continues(world):
    signals = {"shell": True}
    d = ClaudeCodeDetector(find_processes_fn=lambda: [object()],
        aggregate_cpu_fn=lambda _: 0, has_active_shell_children_fn=lambda _: signals["shell"],
        shell_cmdline_fn=lambda _: ["pytest"], recent_file_ages_fn=lambda: [],
        glob_fn=lambda _: [Path("/nonexistent/s.jsonl")],
        stat_fn=lambda _: SimpleNamespace(st_mtime=world[0]))
    sm = watcher.StateMachine(detectors=[d])
    assert sm.compute(notify=False).state == "working"
    signals["shell"] = False
    world[0] += 24.999
    assert sm.compute(notify=False).state == "working"
    world[0] += .001
    assert sm.compute(notify=False).state == "thinking"


def test_killed_claude_turn_cannot_borrow_live_codex_process(world):
    flag(watcher.CLAUDE_TURN_ACTIVE_DIR, "killed-session", world[0] - 60)
    sm = watcher.StateMachine(detectors=[agent("claude", running=False, age=60), agent("codex")])
    assert sm.compute(notify=False).state == "idle"


@pytest.mark.parametrize("transcript_age", [float("inf"), 600.0])
def test_new_prompt_starts_thinking_before_first_transcript_write(world, transcript_age):
    flag(watcher.CLAUDE_TURN_ACTIVE_DIR, "new-turn", world[0])
    sm = watcher.StateMachine(detectors=[agent("claude", age=transcript_age)])
    assert sm.compute(notify=False).state == "thinking"
    world[0] += 180.001
    assert sm.compute(notify=False).state == "idle"
