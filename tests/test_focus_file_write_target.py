"""A project file write carries no writer identity (an mtime, not an owner), so
with no shell owner the working state names no focus target: a recent
transcript write, a sole active turn or a sole open helper does not prove which
session -- or a human editor -- wrote the file. A shell owner still wins."""
from __future__ import annotations

from pathlib import Path

from squid_pet import detectors, watcher
from squid_pet.detectors import ClaudeCodeDetector, CodexDetector
from squid_pet.watcher import StateMachine

_ROOT = "/fake/.claude/projects/-Users-me-proj/"
_NOW = 1_000_000.0


def _machine(monkeypatch, transcripts, *, codex_file=False, shell_owner=None,
             turn_sessions=()):
    """Claude sees a project file write; `transcripts` is {path: age_sec}."""
    monkeypatch.setattr(watcher, "macos_idle_seconds", lambda: 0.0)
    for name in ("CLAUDE_AWAITING_INPUT_DIR", "CLAUDE_FINISHED_DIR",
                 "CLAUDE_RECAPPING_DIR", "CLAUDE_TASK_COMPLETE_DIR",
                 "CLAUDE_TURN_ACTIVE_DIR"):
        monkeypatch.setattr(watcher, name, "/nonexistent")
    ages = {Path(p): a for p, a in transcripts.items()}

    def _stat(p):
        class _S:
            st_mtime = _NOW - ages.get(Path(p), 0.0)
        return _S()

    claude = ClaudeCodeDetector(
        find_processes_fn=lambda: ["fake-claude-proc"],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: shell_owner is not None,
        projects_dir=Path("/fake/.claude/projects"),
        glob_fn=lambda root: iter(list(ages)),
        stat_fn=_stat,
        recent_file_ages_fn=lambda: [1.0],
        active_sessions_fn=lambda: set(turn_sessions),
        finished_mtime_fn=lambda sid: None,
    )
    if shell_owner is not None:
        def _signals(procs, active_fn, cmdline_fn, owner_out=None):
            owner_out.update(shell_owner)
            return True, ["sleep"]
        monkeypatch.setattr(detectors, "_shell_signals", _signals)
    dets = [claude]
    if codex_file:
        dets.append(CodexDetector(
            find_processes_fn=lambda: ["fake-codex-proc"],
            aggregate_cpu_fn=lambda p: 0.0,
            has_active_shell_children_fn=lambda p: False,
            sessions_dir=Path("/fake/.codex/sessions"),
            glob_fn=lambda root: iter([]),
            recent_file_ages_fn=lambda: [1.0],
        ))
    monkeypatch.setattr(watcher.time, "time", lambda: _NOW)
    monkeypatch.setattr(watcher, "claude_turn_active_sessions",
                        lambda fresh_sec=None: list(turn_sessions))
    return StateMachine(detectors=dets)


def test_file_write_with_one_recent_session_stays_untargeted(monkeypatch):
    # A recent transcript write does not prove that session wrote the file:
    # another session's in-process edit or a human editor bumps the same mtime.
    sm = _machine(monkeypatch, {_ROOT + "PARENT/subagents/agent-x.jsonl": 14.0})
    st = sm.compute(notify=False)
    assert st.state == "working"
    assert st.focus_target is None


def test_file_write_with_two_recent_sessions_stays_untargeted(monkeypatch):
    sm = _machine(monkeypatch, {
        _ROOT + "A.jsonl": 14.0,
        _ROOT + "B/subagents/agent-y.jsonl": 15.0,
    })
    st = sm.compute(notify=False)
    assert st.state == "working"
    assert st.focus_target is None


def test_file_write_also_seen_by_codex_stays_untargeted(monkeypatch):
    sm = _machine(monkeypatch, {_ROOT + "A.jsonl": 14.0}, codex_file=True)
    assert sm.compute(notify=False).focus_target is None


def test_file_write_with_sole_active_turn_stays_untargeted(monkeypatch):
    sm = _machine(monkeypatch, {_ROOT + "OLD.jsonl": 300.0},
                  turn_sessions=["TURN-SID"])
    st = sm.compute(notify=False)
    assert st.state == "working"
    assert st.focus_target is None


def test_file_write_with_two_active_turns_stays_untargeted(monkeypatch):
    sm = _machine(monkeypatch, {_ROOT + "OLD.jsonl": 300.0},
                  turn_sessions=["T1", "T2"])
    assert sm.compute(notify=False).focus_target is None


def test_file_write_with_single_open_helper_stays_untargeted(monkeypatch):
    monkeypatch.setattr(ClaudeCodeDetector, "_subagent_is_open",
                        lambda self, path, stat: True)
    sm = _machine(monkeypatch, {_ROOT + "A/subagents/agent-A.jsonl": 100.0},
                  turn_sessions=["A"])
    st = sm.compute(notify=False)
    assert sm._claude_detector.subagent_open is True  # the helper IS held open
    assert st.state == "working"
    assert st.focus_target is None


def test_shell_owner_still_wins_over_file_write(monkeypatch):
    owner = {"pid": 7, "created": 5.0}
    sm = _machine(monkeypatch, {_ROOT + "A.jsonl": 300.0}, shell_owner=owner)
    st = sm.compute(notify=False)
    assert st.focus_target == {"agent": "claude", "owner": owner}
