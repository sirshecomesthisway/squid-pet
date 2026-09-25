"""A2 (2026-09-24): take-me-there must reach the right tab when a background
subagent (Task-tool helper) is what's driving working/thinking.

After the parent's Stop, CLAUDE_TURN_ACTIVE_DIR is empty, so the working/
thinking signal-dir lookup names no session and focus used to blind-raise
*some* claude window (the wrong one with several sessions running). The
watcher now threads the owning (parent) session id -- derived from the live
transcript path -- into the snapshot's focus_target, and focus resolves that
session's tab directly.
"""
from __future__ import annotations

from pathlib import Path

from squid_pet import focus, watcher
from squid_pet.detectors import (
    ClaudeCodeDetector,
    claude_session_id_from_transcript,
)
from squid_pet.watcher import StateMachine


# ── the path -> owning session helper ──────────────────────────────────
def test_parent_transcript_session_is_the_stem():
    p = "/Users/me/.claude/projects/-Users-me-proj/1234-abcd.jsonl"
    assert claude_session_id_from_transcript(p) == "1234-abcd"


def test_subagent_transcript_session_is_the_owning_dir():
    p = ("/Users/me/.claude/projects/-Users-me-proj/"
         "1234-abcd/subagents/agent-a0e33328136f521e9.jsonl")
    assert claude_session_id_from_transcript(p) == "1234-abcd"


def test_none_path_yields_none():
    assert claude_session_id_from_transcript(None) is None


# ── the watcher threads the session into the focus target ──────────────
def _install(monkeypatch):
    monkeypatch.setattr(watcher, "macos_idle_seconds", lambda: 0.0)
    monkeypatch.setattr(watcher, "CLAUDE_AWAITING_INPUT_DIR", "/nonexistent")
    monkeypatch.setattr(watcher, "CLAUDE_FINISHED_DIR", "/nonexistent")
    monkeypatch.setattr(watcher, "CLAUDE_RECAPPING_DIR", "/nonexistent")
    monkeypatch.setattr(watcher, "CLAUDE_TASK_COMPLETE_DIR", "/nonexistent")
    monkeypatch.setattr(watcher, "CLAUDE_TURN_ACTIVE_DIR", "/nonexistent")


def test_thinking_snapshot_threads_owning_session_from_subagent_path(monkeypatch):
    _install(monkeypatch)
    now = 1_000_000.0
    sub = "/fake/.claude/projects/-Users-me-proj/PARENT-SID/subagents/agent-x.jsonl"

    def _stat(p):
        class _S:
            st_mtime = now - 2.0
        return _S()

    claude = ClaudeCodeDetector(
        find_processes_fn=lambda: ["fake-claude-proc"],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=Path("/fake/.claude/projects"),
        glob_fn=lambda root: iter([Path(sub)]),
        stat_fn=_stat,
        recent_file_ages_fn=lambda: [],
    )
    sm = StateMachine(detectors=[claude])
    monkeypatch.setattr(watcher.time, "time", lambda: now)

    st = sm.compute()
    assert st.state == "thinking"
    assert st.focus_target == {"agent": "claude", "session": "PARENT-SID"}


# ── focus resolves the helper's parent session, not another session ────
class _FakeProc:
    def __init__(self, tty):
        self._tty = tty

    def terminal(self):
        return self._tty


def test_focus_resolves_helper_parent_session_after_turn_close(monkeypatch):
    """The turn-active dir is empty (parent Stop cleared it) but the snapshot
    threaded the parent session. Focus must raise THAT session's tab, not the
    session-blind first-found claude window (a different, healthy session)."""
    state = watcher.PetState(
        state="thinking",
        focus_target={"agent": "claude", "session": "PARENT-SID"},
    )
    # signal-dir lookups find nothing -- the turn-active flag is gone.
    monkeypatch.setattr(focus, "_named_if_fresh",
                        lambda _dir, _name, _fresh=None: None)
    monkeypatch.setattr(focus, "_freshest_in", lambda _dir, _fresh=None: None)
    # The authoritative session -> process/tty map.
    monkeypatch.setattr(watcher, "claude_session_proc",
                        lambda sid: _FakeProc("/dev/ttysPARENT")
                        if sid == "PARENT-SID" else None)
    monkeypatch.setattr(watcher, "_terminal_app_bundle_for_proc",
                        lambda _proc: focus.TERMINAL_APP_BUNDLE_ID)
    # If A2 failed and the blind fallback ran, it would pick this OTHER tty.
    monkeypatch.setattr(watcher, "find_claude_code_processes",
                        lambda: [_FakeProc("/dev/ttysOTHER")])

    seen = []
    result = focus.focus_for_snapshot(
        state, run=lambda script: seen.append(script) or "matched")
    assert result == "matched"
    assert seen and "ttysPARENT" in seen[0]
    assert "ttysOTHER" not in seen[0]


def test_focus_without_threaded_session_still_blind_falls_back(monkeypatch):
    """Control: when no session is threaded (e.g. an older snapshot) the
    documented blind fallback is unchanged -- it raises some claude window."""
    state = watcher.PetState(state="thinking", focus_target={"agent": "claude"})
    monkeypatch.setattr(focus, "_named_if_fresh",
                        lambda _dir, _name, _fresh=None: None)
    monkeypatch.setattr(focus, "_freshest_in", lambda _dir, _fresh=None: None)
    monkeypatch.setattr(watcher, "find_claude_code_processes",
                        lambda: [_FakeProc("/dev/ttysOTHER")])
    monkeypatch.setattr(watcher, "find_terminal_app_bundle_for_claude_code",
                        lambda: focus.TERMINAL_APP_BUNDLE_ID)

    seen = []
    result = focus.focus_for_snapshot(
        state, run=lambda script: seen.append(script) or "matched")
    # Blind fallback picks the only live claude tty.
    assert result == "matched"
    assert seen and "ttysOTHER" in seen[0]


# ── finding 2: a working target's owner is authoritative, not the newest ──
# transcript. Scenario: session A runs a shell command while session B has the
# newest transcript. working_target must attach A's shell owner and NOT B's
# transcript-derived session, and focus must resolve A's exact process -- not
# raise B's tab or blind-raise some other window.
class _FakeClaude:
    name = "claude_code"
    enabled = True
    claude_code_running = True
    shell_active = True
    file_active = False
    streaming = False
    transcript_age = 2.0
    # newest transcript belongs to session B, NOT the shell owner (session A)
    transcript_path = "/fake/.claude/projects/-Users-me-proj/SESSION-B.jsonl"

    def __init__(self, owner):
        self.shell_owner = owner

    def is_busy(self, _now):
        return self.shell_active or self.file_active or self.streaming

    def is_celebrating(self, _now):
        return False


def _install_full(monkeypatch):
    from squid_pet import codex_turns
    _install(monkeypatch)
    monkeypatch.setattr(watcher, "claude_sessions_awaiting_input", lambda: [])
    monkeypatch.setattr(watcher, "codex_requests_awaiting_input", lambda: [])
    monkeypatch.setattr(watcher.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(codex_turns, "active_turns", lambda _now: [])


def test_working_target_uses_shell_owner_not_transcript_session(monkeypatch):
    owner = {"pid": 4242, "created": 50.0}
    _install_full(monkeypatch)
    sm = StateMachine(detectors=[_FakeClaude(owner)])
    st = sm.compute(notify=False)
    assert st.state == "working"
    assert st.focus_target == {"agent": "claude", "owner": owner}
    assert "session" not in st.focus_target


class _FakeOwnerProc:
    def __init__(self, pid, created, tty):
        self.pid = pid
        self._created = created
        self._tty = tty

    def create_time(self):
        return self._created

    def is_running(self):
        return True

    def status(self):
        import psutil
        return psutil.STATUS_RUNNING

    def terminal(self):
        return self._tty


def test_working_target_owner_resolves_exact_process_tab(monkeypatch):
    """A claude working target carrying an owner (no session) must resolve the
    owner's EXACT process to its tab -- before any blind fallback that would
    pick another live session's window."""
    import psutil
    owner = {"pid": 4242, "created": 50.0}
    state = watcher.PetState(
        state="working", focus_target={"agent": "claude", "owner": owner})
    proc = _FakeOwnerProc(4242, 50.0, "/dev/ttysOWNER")

    def _process(pid):
        if pid == 4242:
            return proc
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", _process)
    monkeypatch.setattr(watcher, "_terminal_app_bundle_for_proc",
                        lambda _p: focus.TERMINAL_APP_BUNDLE_ID)
    monkeypatch.setattr(focus, "_freshest_in", lambda _dir, _fresh=None: None)
    # If owner resolution failed, the blind fallback would pick this OTHER tty.
    monkeypatch.setattr(watcher, "find_claude_code_processes",
                        lambda: [_FakeProc("/dev/ttysOTHER")])

    seen = []
    result = focus.focus_for_snapshot(
        state, run=lambda script: seen.append(script) or "matched")
    assert result == "matched"
    assert seen and "ttysOWNER" in seen[0]
    assert "ttysOTHER" not in seen[0]


def test_working_target_dead_owner_falls_back(monkeypatch):
    """If the owner process is gone, focus falls through to the documented
    blind fallback rather than returning nothing."""
    import psutil
    owner = {"pid": 4242, "created": 50.0}
    state = watcher.PetState(
        state="working", focus_target={"agent": "claude", "owner": owner})

    def _process(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", _process)
    monkeypatch.setattr(focus, "_named_if_fresh",
                        lambda _dir, _name, _fresh=None: None)
    monkeypatch.setattr(focus, "_freshest_in", lambda _dir, _fresh=None: None)
    monkeypatch.setattr(watcher, "find_claude_code_processes",
                        lambda: [_FakeProc("/dev/ttysOTHER")])
    monkeypatch.setattr(watcher, "find_terminal_app_bundle_for_claude_code",
                        lambda: focus.TERMINAL_APP_BUNDLE_ID)
    seen = []
    result = focus.focus_for_snapshot(
        state, run=lambda script: seen.append(script) or "matched")
    assert result == "matched"
    assert seen and "ttysOTHER" in seen[0]


def test_concerned_session_still_gated_by_freshness(monkeypatch):
    """A2's direct-trust is scoped to working/thinking. `concerned` (session
    from a flag dir, not a transcript) keeps its cross-source freshness guard:
    a session whose flag aged out must not be trusted, so with nothing fresh
    it falls through to resting -- unchanged behaviour."""
    state = watcher.PetState(
        state="concerned",
        focus_target={"agent": "claude", "session": "STALE-SID"},
    )
    monkeypatch.setattr(focus, "_named_if_fresh",
                        lambda _dir, _name, _fresh=None: None)
    monkeypatch.setattr(focus, "_freshest_in", lambda _dir, _fresh=None: None)
    # Would resolve if A2 wrongly trusted it -- it must NOT be consulted.
    called = []
    monkeypatch.setattr(watcher, "claude_session_proc",
                        lambda sid: called.append(sid) or _FakeProc("/dev/ttysX"))

    result = focus.focus_for_snapshot(
        state, run=lambda script: (_ for _ in ()).throw(AssertionError()))
    assert result == "resting"
    assert called == []
