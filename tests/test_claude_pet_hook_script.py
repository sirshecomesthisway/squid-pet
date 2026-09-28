"""Pink-2026-08-26: exercises scripts/claude_pet_hook.py as a REAL
subprocess (it's invoked by the Claude Code CLI as an external command,
never imported), the same way it actually runs in production. Verifies
the on-disk flag-file protocol that watcher.py's
claude_sessions_awaiting_input() reads.

SQUID_PET_HOME is overridden per-test to a tmp_path so nothing here ever
touches the real ~/.squid-pet.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "claude_pet_hook.py"


def _run(payload: dict | None, home: Path) -> subprocess.CompletedProcess:
    stdin_data = "" if payload is None else json.dumps(payload)
    env = dict(os.environ)
    env["SQUID_PET_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin_data, capture_output=True, text=True, timeout=10, env=env,
    )


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "squid-pet-home"


def _flag_path(home: Path, session_id: str) -> Path:
    return home / "claude_awaiting_input" / session_id


def _finished_path(home: Path, session_id: str) -> Path:
    return home / "claude_finished" / session_id


def _turn_active_path(home: Path, session_id: str) -> Path:
    return home / "claude_turn_active" / session_id


def _recap_path(home: Path, session_id: str) -> Path:
    return home / "claude_recapping" / session_id


def _failed_path(home: Path, session_id: str) -> Path:
    return home / "claude_failed" / session_id


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


def test_notification_permission_prompt_writes_flag(home):
    r = _run({
        "session_id": "sess-1", "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
        "message": "Claude needs your permission",
    }, home)
    assert r.returncode == 0, r.stderr
    fp = _flag_path(home, "sess-1")
    assert fp.exists()
    # First line: the notification type (the watcher/focus only ever read
    # names + mtimes). Then one owner line: a main-thread prompt is `parent`,
    # stamped with its add time (round 3).
    lines = fp.read_text().splitlines()
    assert lines[0] == "permission_prompt"
    assert len(lines) == 2 and lines[1].split()[0] == "parent"
    assert _owners(home, "sess-1") == {"parent"}


def test_notification_idle_prompt_is_ignored(home):
    """Pink-2026-09-01: idle_prompt fires 60s after Claude hands control
    back and simply means "it is your turn and you are not here". Pink:
    "I don't need her to tell me what to do next, only to speak up when
    she needs me." Only permission_prompt -- an actual blocked request --
    is worth interrupting for now.

    Note this does NOT weaken the stepped-away case: a permission prompt
    raised while you are away still waves and still fires the banner. What
    is gone is the alert that fires when nothing is blocked at all."""
    r = _run({
        "session_id": "sess-2", "hook_event_name": "Notification",
        "notification_type": "idle_prompt",
    }, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-2").exists()


def test_notification_unhandled_type_does_not_write_flag(home):
    """auth_success and any other type we haven't confirmed the meaning
    of must NOT create a flag -- only the two empirically-confirmed
    'waiting on you' types should."""
    r = _run({
        "session_id": "sess-3", "hook_event_name": "Notification",
        "notification_type": "auth_success",
    }, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-3").exists()


def test_user_prompt_submit_removes_flag(home):
    _run({"session_id": "sess-4", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    assert _flag_path(home, "sess-4").exists()

    r = _run({"session_id": "sess-4", "hook_event_name": "UserPromptSubmit"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-4").exists()


# ── PreToolUse: capture "blocked on the user" moments that emit no
#    Notification (2026-09-06). AskUserQuestion and ExitPlanMode block for
#    a human answer/approval but fire no permission_prompt -- only an
#    idle_prompt after ~60s, which is ignored. They ARE tools, so
#    PreToolUse fires right before they block, carrying tool_name.
def test_pretooluse_askuserquestion_writes_flag(home):
    r = _run({
        "session_id": "sess-q", "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
    }, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-q").exists()


def test_pretooluse_exitplanmode_writes_flag(home):
    r = _run({
        "session_id": "sess-p", "hook_event_name": "PreToolUse",
        "tool_name": "ExitPlanMode",
    }, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-p").exists()


def test_pretooluse_ordinary_tool_does_not_write_flag(home):
    """PreToolUse fires before EVERY tool. Only the ones that block on a
    human answer should raise the attention flag -- a Bash/Read/Edit call
    is Claude working, not waiting."""
    r = _run({
        "session_id": "sess-b", "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
    }, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-b").exists()


def test_pretooluse_question_then_answer_brackets_the_flag(home):
    """The whole point: flag is up only while she's actually waiting.
    PreToolUse(AskUserQuestion) raises it; answering fires
    PostToolUse(AskUserQuestion), which clears it (PostToolUse is already
    a remove-event)."""
    _run({"session_id": "sess-br", "hook_event_name": "PreToolUse",
          "tool_name": "AskUserQuestion"}, home)
    assert _flag_path(home, "sess-br").exists()

    r = _run({"session_id": "sess-br", "hook_event_name": "PostToolUse",
              "tool_name": "AskUserQuestion"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-br").exists()


def test_session_end_removes_flag(home):
    _run({"session_id": "sess-5", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    assert _flag_path(home, "sess-5").exists()

    r = _run({"session_id": "sess-5", "hook_event_name": "SessionEnd"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-5").exists()


# ── Stop (Pink-2026-08-27f: replaces the busy->idle celebrate heuristic
# with the real "Claude finished responding" signal -- see
# watcher.claude_sessions_just_finished()) ──────────────────────────────
def test_stop_writes_finished_flag(home):
    r = _run({"session_id": "sess-6", "hook_event_name": "Stop",
              "last_assistant_message": "done!"}, home)
    assert r.returncode == 0, r.stderr
    fp = _finished_path(home, "sess-6")
    assert fp.exists()


def test_stop_does_not_read_last_assistant_message_into_the_flag(home):
    """Privacy stance: session_id and mtime only, never message content --
    matches the awaiting-input flag's own content-blind contract."""
    r = _run({"session_id": "sess-7", "hook_event_name": "Stop",
              "last_assistant_message": "some potentially sensitive text"}, home)
    assert r.returncode == 0, r.stderr
    fp = _finished_path(home, "sess-7")
    assert "sensitive" not in fp.read_text()


def test_stop_does_not_create_an_awaiting_input_flag(home):
    """Stop must never CREATE an awaiting-input flag -- "the turn ended" is
    not "the session is waiting on you"; only a Notification says that.

    Pink-2026-08-31: this used to assert Stop never removed one either, but
    that invariant was wrong and caused a real stuck-wave bug -- see
    test_stop_clears_a_stuck_awaiting_input_flag below for why Stop now
    clears. The no-create half is unchanged and still load-bearing.
    """
    r = _run({"session_id": "sess-8", "hook_event_name": "Stop"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-8").exists()


# ── Answering a permission prompt (Pink-2026-08-31) ────────────────────
# Answering a permission prompt -- yes OR no -- fires no hook of its own.
# With only UserPromptSubmit/SessionEnd clearing the flag, approving a
# prompt and letting Claude carry on left it set indefinitely: confirmed
# live in claude_hook.log (session 8ced357d) as a nine-minute wave at a
# session that was happily working, with approval_needed's PRIME priority
# masking every working/thinking state behind it the whole time.
def test_post_tool_use_clears_the_awaiting_input_flag(home):
    """The APPROVAL path: a tool actually executed, so whatever permission
    question was pending has been answered and the session is no longer
    blocked on the user."""
    _run({"session_id": "sess-p1", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    assert _flag_path(home, "sess-p1").exists()

    r = _run({"session_id": "sess-p1", "hook_event_name": "PostToolUse",
              "tool_name": "Bash"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-p1").exists()


def test_stop_clears_a_stuck_awaiting_input_flag(home):
    """The DENIAL path: denying runs no tool, so PostToolUse never fires.
    Stop is what covers it -- control came back, so nothing is blocked on a
    dialog any more."""
    _run({"session_id": "sess-p2", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    assert _flag_path(home, "sess-p2").exists()

    r = _run({"session_id": "sess-p2", "hook_event_name": "Stop"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-p2").exists()
    # Stop's own signal must still land -- clearing is additive, not a swap.
    assert _finished_path(home, "sess-p2").exists()


def test_a_later_permission_prompt_rearms_after_stop_cleared_the_flag(home):
    """Clearing on Stop must not permanently suppress the next wave: a
    permission prompt raised afterwards has to be able to re-arm the flag
    Stop cleared."""
    _run({"session_id": "sess-p3", "hook_event_name": "Stop"}, home)
    assert not _flag_path(home, "sess-p3").exists()

    _run({"session_id": "sess-p3", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    assert _flag_path(home, "sess-p3").exists()


def test_post_tool_use_on_nonexistent_flag_is_a_noop(home):
    """PostToolUse fires on EVERY tool call, so the overwhelmingly common
    case is no flag to clear. It must be silent and cheap, never an error."""
    r = _run({"session_id": "sess-p4", "hook_event_name": "PostToolUse",
              "tool_name": "Read"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-p4").exists()


def test_post_tool_use_only_clears_its_own_session(home):
    """The reason this beats watcher.py's self-heal, which is gated on
    len(find_claude_code_processes()) <= 1 precisely because it cannot tell
    whose activity resolved whose wait: the hook payload carries the
    session_id, so a busy session can never cancel another one's wave."""
    for sid in ("sess-p5", "sess-p6"):
        _run({"session_id": sid, "hook_event_name": "Notification",
              "notification_type": "permission_prompt"}, home)

    _run({"session_id": "sess-p5", "hook_event_name": "PostToolUse",
          "tool_name": "Bash"}, home)

    assert not _flag_path(home, "sess-p5").exists()
    assert _flag_path(home, "sess-p6").exists(), (
        "another session's genuinely-pending wave must survive")


# ── subagent (Task-tool helper) events must not touch parent state ──────
# Review finding 2: a subagent's PreToolUse/PostToolUse carry the PARENT
# session_id plus an agent_id. Because PostToolUse is a remove-on-event, a
# helper's tool call used to clear the parent's awaiting-input flag while the
# parent was still blocked on the user (e.g. an AskUserQuestion), dropping the
# wave. A helper event (agent_id present) must leave the parent's flag alone.
def test_helper_post_tool_use_does_not_clear_awaiting_flag(home):
    """The parent is blocked on the user; a background helper's PostToolUse
    (carrying agent_id) must NOT clear the parent's awaiting-input flag."""
    # Parent blocks on an AskUserQuestion.
    _run({"session_id": "sess-h1", "hook_event_name": "PreToolUse",
          "tool_name": "AskUserQuestion"}, home)
    assert _flag_path(home, "sess-h1").exists()

    # A background subagent tool call fires PostToolUse under the SAME parent
    # session_id, but with an agent_id -- it must not clear the wave.
    r = _run({"session_id": "sess-h1", "hook_event_name": "PostToolUse",
              "tool_name": "Bash", "agent_id": "a0e33328136f521e9",
              "agent_type": "general-purpose"}, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-h1").exists(), (
        "a helper's PostToolUse must not clear the parent's pending wave")


def test_main_thread_post_tool_use_still_clears_awaiting_flag(home):
    """The main thread answering the question fires PostToolUse with NO
    agent_id -- that must still clear the flag (unchanged behaviour)."""
    _run({"session_id": "sess-h2", "hook_event_name": "PreToolUse",
          "tool_name": "AskUserQuestion"}, home)
    assert _flag_path(home, "sess-h2").exists()

    r = _run({"session_id": "sess-h2", "hook_event_name": "PostToolUse",
              "tool_name": "AskUserQuestion"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-h2").exists()


def test_helper_post_tool_use_tagged_in_log(home):
    """The skip is tagged in the log so helper events are distinguishable
    (content-blind: no tool payload logged)."""
    _run({"session_id": "sess-h3", "hook_event_name": "PreToolUse",
          "tool_name": "AskUserQuestion"}, home)
    _run({"session_id": "sess-h3", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "a0e33328136f521e9",
          "tool_input": {"command": "rm -rf /secret/path"}}, home)
    log_text = (home / "claude_hook.log").read_text()
    assert "HELPER" in log_text
    assert "secret" not in log_text


# ── helper PreToolUse must not raise the parent's wave (finding 1) ──────
# A plan-mode subagent fires PreToolUse ExitPlanMode carrying the PARENT
# session_id + agent_id. Its later PostToolUse is HELPER_SKIPped, so if the
# PreToolUse were allowed to write claude_awaiting_input/<parent sid> the false
# wave would never clear. A helper PreToolUse must write nothing. (AskUserQuestion
# can't happen in a subagent; ExitPlanMode can.)
def test_helper_pre_tool_use_exit_plan_mode_writes_nothing(home):
    r = _run({"session_id": "sess-p1", "hook_event_name": "PreToolUse",
              "tool_name": "ExitPlanMode", "agent_id": "a0e33328136f521e9",
              "agent_type": "general-purpose"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-p1").exists(), (
        "a helper's PreToolUse must not raise the parent's wave")


def test_main_thread_pre_tool_use_exit_plan_mode_still_writes(home):
    """The main thread (no agent_id) entering plan mode still waves."""
    r = _run({"session_id": "sess-p2", "hook_event_name": "PreToolUse",
              "tool_name": "ExitPlanMode"}, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-p2").exists()


def test_main_thread_pre_tool_use_ask_user_question_still_writes(home):
    """The main thread (no agent_id) asking a question still waves."""
    r = _run({"session_id": "sess-p3", "hook_event_name": "PreToolUse",
              "tool_name": "AskUserQuestion"}, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-p3").exists()


def test_helper_pre_tool_use_tagged_helper_skip_in_log(home):
    r = _run({"session_id": "sess-p4", "hook_event_name": "PreToolUse",
              "tool_name": "ExitPlanMode", "agent_id": "a0e33328136f521e9"}, home)
    assert r.returncode == 0, r.stderr
    log_text = (home / "claude_hook.log").read_text()
    assert "PreToolUse sess-p4 HELPER_SKIP" in log_text


def test_helper_notification_permission_prompt_still_waves(home):
    """A helper's permission_prompt is a genuine 'blocked on the user' moment
    (Claude Code surfaces background-helper prompts in the main session), so the
    Notification branch must NOT be gated on agent_id -- it still writes."""
    r = _run({"session_id": "sess-p5", "hook_event_name": "Notification",
              "notification_type": "permission_prompt",
              "agent_id": "a0e33328136f521e9"}, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-p5").exists()


# ── defence-in-depth: a helper event mutates NO parent state (finding A) ──
# Round-2 finding: a helper (agent_id) event was skipped only in the FIRST
# dispatch chain, then fell through into the second (Stop/StopFailure/PreCompact/
# PostCompact + clear-failed) and third (turn open/close) chains. An agent_id
# Stop would still write claude_finished/<parent> and close claude_turn_active/
# <parent>; StopFailure would write claude_failed; UserPromptSubmit would open a
# turn and clear failed; PreCompact/PostCompact would touch recapping. A helper
# shares the PARENT session_id, so none of these may fire. (Live: subagents send
# SubagentStop not Stop, but helper StopFailure/compaction are unverified -- this
# is cheap defence in depth.)
_HELPER_MUTATING_EVENTS = [
    "Stop", "StopFailure", "UserPromptSubmit", "PreCompact", "PostCompact",
    "SessionEnd",
]


def _seed_parent_flags(home: Path, sid: str) -> None:
    for sub, content in (
        ("claude_awaiting_input", "permission_prompt"),
        ("claude_turn_active", "turn"),
        ("claude_failed", "overloaded_error"),
        ("claude_recapping", "manual"),
    ):
        d = home / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / sid).write_text(content)


def _snapshot_parent_flags(home: Path, sid: str) -> dict:
    snap = {}
    for sub in ("claude_awaiting_input", "claude_turn_active", "claude_failed",
                "claude_recapping", "claude_finished"):
        p = home / sub / sid
        snap[sub] = p.read_text() if p.exists() else None
    return snap


@pytest.mark.parametrize("event", _HELPER_MUTATING_EVENTS)
def test_helper_event_mutates_no_parent_state(home, event):
    _seed_parent_flags(home, "sess-hx")
    before = _snapshot_parent_flags(home, "sess-hx")
    r = _run({"session_id": "sess-hx", "hook_event_name": event,
              "agent_id": "a0e33328136f521e9", "agent_type": "general-purpose",
              "error_type": "overloaded_error", "trigger": "manual"}, home)
    assert r.returncode == 0, r.stderr
    after = _snapshot_parent_flags(home, "sess-hx")
    assert after == before, f"{event} with agent_id mutated parent state"
    # claude_finished must never be created off a helper event.
    assert after["claude_finished"] is None
    # ...and it is logged content-blind as a helper skip.
    log_text = (home / "claude_hook.log").read_text()
    assert f"{event} sess-hx HELPER_SKIP" in log_text


@pytest.mark.parametrize("event", _HELPER_MUTATING_EVENTS)
def test_same_events_without_agent_id_still_mutate(home, event):
    """Control: identical events with NO agent_id behave exactly as before --
    the gate is agent_id, not the event name."""
    _seed_parent_flags(home, "sess-mx")
    r = _run({"session_id": "sess-mx", "hook_event_name": event,
              "error_type": "overloaded_error", "trigger": "manual"}, home)
    assert r.returncode == 0, r.stderr
    after = _snapshot_parent_flags(home, "sess-mx")
    if event == "Stop":
        assert after["claude_finished"] == "stop"          # written
        assert after["claude_turn_active"] is None          # turn closed
    elif event == "StopFailure":
        assert after["claude_failed"] == "overloaded_error"  # written
        assert after["claude_turn_active"] is None           # turn closed
    elif event == "UserPromptSubmit":
        assert after["claude_turn_active"] == "turn"         # (re)opened
        assert after["claude_failed"] is None                # cleared
        assert after["claude_awaiting_input"] is None        # remove-on-event
    elif event == "PreCompact":
        assert after["claude_recapping"] == "manual"         # written
    elif event == "PostCompact":
        assert after["claude_recapping"] is None             # removed
    elif event == "SessionEnd":
        assert after["claude_turn_active"] is None           # closed
        assert after["claude_recapping"] is None             # cleared
        assert after["claude_failed"] is None                # cleared


def test_helper_notification_permission_prompt_still_waves_after_gate(home):
    """The one ungated helper path: a permission_prompt (even a background
    helper's, which Claude Code surfaces in the main session) still writes."""
    r = _run({"session_id": "sess-nh", "hook_event_name": "Notification",
              "notification_type": "permission_prompt",
              "agent_id": "a0e33328136f521e9"}, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-nh").exists()


# ── a helper's OWN approval must clear ITS OWN wave (root-cause: 3193981
# left no path for this at all -- a subagent's permission_prompt waves the
# parent per the Notification exception above, but the helper's own
# PostToolUse/SubagentStop was then unconditionally HELPER_SKIPped, so the
# wave only ever cleared on the PARENT's next UserPromptSubmit/Stop --
# observed ~100s late live). The flag is now an owner set: every raise adds
# its own owner line (`parent` or `agent:<agent_id>`), and every clearing
# event removes ONLY the caller's own line (round 2: under an fcntl.flock).
def test_helper_post_tool_use_clears_its_own_prompt(home):
    """The helper that raised the prompt gets its own tool call approved
    (PostToolUse carrying the SAME agent_id) -- that must clear the flag
    it itself raised."""
    r = _run({"session_id": "sess-o1", "hook_event_name": "Notification",
              "notification_type": "permission_prompt",
              "agent_id": "agent-aaa"}, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-o1").exists()

    r = _run({"session_id": "sess-o1", "hook_event_name": "PostToolUse",
              "tool_name": "Bash", "agent_id": "agent-aaa"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-o1").exists(), (
        "the helper that raised the prompt must be able to clear its own wave")


def test_different_helper_post_tool_use_does_not_clear(home):
    """A DIFFERENT helper's PostToolUse (different agent_id, same parent
    session_id) must NOT clear a wave it did not raise."""
    _run({"session_id": "sess-o2", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    assert _flag_path(home, "sess-o2").exists()

    r = _run({"session_id": "sess-o2", "hook_event_name": "PostToolUse",
              "tool_name": "Bash", "agent_id": "agent-bbb"}, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-o2").exists(), (
        "a different helper's approval must not clear another helper's wave")


def test_helper_post_tool_use_does_not_clear_a_parent_owned_prompt(home):
    """3193981 regression guard: a PARENT's own permission prompt (no
    agent_id -- e.g. a top-level tool call, or Notification fired before any
    helper existed) has no recorded owner, so no helper may ever clear it."""
    _run({"session_id": "sess-o3", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    assert _flag_path(home, "sess-o3").exists()

    r = _run({"session_id": "sess-o3", "hook_event_name": "PostToolUse",
              "tool_name": "Bash", "agent_id": "agent-aaa"}, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-o3").exists(), (
        "a helper must never clear a parent-owned (unowned) wave")


def test_parent_post_tool_use_still_clears_parent_owned_prompt(home):
    """Unchanged behaviour: the parent's own PostToolUse (no agent_id)
    clears its own parent-owned prompt."""
    _run({"session_id": "sess-o4", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    assert _flag_path(home, "sess-o4").exists()

    r = _run({"session_id": "sess-o4", "hook_event_name": "PostToolUse",
              "tool_name": "Bash"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-o4").exists()


def test_helper_subagent_stop_clears_its_own_prompt_denial_path(home):
    """A denied prompt never fires PostToolUse (no tool ran). SubagentStop
    -- the helper's own turn-ended event -- is the helper's analogue of the
    parent's Stop (already a _REMOVE_ON_EVENTS member), so it must clear a
    prompt the SAME agent_id raised, covering denial-without-approval."""
    _run({"session_id": "sess-o5", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    assert _flag_path(home, "sess-o5").exists()

    r = _run({"session_id": "sess-o5", "hook_event_name": "SubagentStop",
              "agent_id": "agent-aaa"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-o5").exists()


def test_helper_own_clear_is_logged_distinctly(home):
    """The clear is content-blind and distinguishable in the log from a
    plain HELPER_SKIP."""
    _run({"session_id": "sess-o6", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    _run({"session_id": "sess-o6", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-aaa",
          "tool_input": {"command": "rm -rf /secret/path"}}, home)
    log_text = (home / "claude_hook.log").read_text()
    assert "sess-o6" in log_text
    assert "secret" not in log_text
    assert "HELPER_CLEAR_OWN" in log_text


def test_helper_own_clear_does_not_reach_second_or_third_chain(home):
    """Defence in depth (same spirit as 3193981's round-2 finding): a
    helper's own-clearing event still must not write claude_finished, open/
    close the turn bracket, or touch recapping/failed under the parent's id
    -- ONLY the awaiting-input flag it owns may move."""
    _seed_parent_flags(home, "sess-o7")
    # Re-tag the awaiting_input flag as helper-owned so the clear path is
    # actually exercised (the seeded one from _seed_parent_flags is unowned).
    (home / "claude_awaiting_input" / "sess-o7").write_text(
        "permission_prompt\nagent:agent-aaa")
    before = _snapshot_parent_flags(home, "sess-o7")

    r = _run({"session_id": "sess-o7", "hook_event_name": "Stop",
              "agent_id": "agent-aaa"}, home)
    assert r.returncode == 0, r.stderr
    after = _snapshot_parent_flags(home, "sess-o7")

    assert after["claude_awaiting_input"] is None, "the owned flag must clear"
    for key in ("claude_turn_active", "claude_failed", "claude_recapping"):
        assert after[key] == before[key], f"{key} must be untouched by a helper Stop"
    assert after["claude_finished"] is None, (
        "a helper Stop must never write claude_finished under the parent's id")


def test_two_concurrent_helpers_clear_independently(home):
    """Cohort: two subagents under the SAME parent session_id each raise a
    permission prompt (Task-tool fan-out). The second's write must NOT erase
    the first's ownership, and neither one's resolution may clear a wave the
    OTHER is still genuinely blocked on -- the flag survives until EVERY
    recorded owner has reported back."""
    _run({"session_id": "sess-o8", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    _run({"session_id": "sess-o8", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-bbb"}, home)
    # Both owners recorded; ntype preserved as the first line for watcher reads.
    content = _flag_path(home, "sess-o8").read_text().splitlines()
    assert content[0] == "permission_prompt"
    assert _owners(home, "sess-o8") == {"agent:agent-aaa", "agent:agent-bbb"}

    # First helper resolves -> flag stays (second still pending), only its
    # own owner line is gone.
    _run({"session_id": "sess-o8", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-aaa"}, home)
    assert _flag_path(home, "sess-o8").exists(), (
        "one helper resolving must not clear a wave the sibling still owns")
    content = _flag_path(home, "sess-o8").read_text().splitlines()
    assert content[0] == "permission_prompt"
    assert _owners(home, "sess-o8") == {"agent:agent-bbb"}

    # Second helper resolves -> cohort empty -> flag removed.
    _run({"session_id": "sess-o8", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-bbb"}, home)
    assert not _flag_path(home, "sess-o8").exists(), (
        "flag must clear once the last owner has reported back")


def test_helper_owner_write_leaves_no_phantom_tmp_in_scan_dir(home):
    """watcher.py lists every NON-dot entry in claude_awaiting_input/ as a
    live awaiting session. The atomic tmp+rename write must leave only the
    session-id flag itself -- no leftover `<sid>.<pid>.tmp` sibling that a
    poll tick would read as a phantom session id."""
    _run({"session_id": "sess-o9", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    entries = os.listdir(home / "claude_awaiting_input")
    visible = [n for n in entries if not n.startswith(".")]
    assert visible == ["sess-o9"], (
        f"only the flag itself may be watcher-visible, got {visible!r}")


def _owners(home: Path, sid: str) -> set[str] | None:
    """Owner NAMES of an awaiting flag (None if the flag is absent). Round 3:
    each owner line is `<owner> <add-epoch>`; the epoch is dropped here."""
    fp = _flag_path(home, sid)
    if not fp.exists():
        return None
    return {line.split()[0] for line in fp.read_text().splitlines()[1:]
            if line.strip()}


def test_parent_wave_survives_helper_raising_and_resolving(home):
    """3193981 guard, mixed case: the PARENT is already blocked on its own
    prompt when a concurrently-running helper raises one too. The helper
    ADDS its own owner line; its resolution removes only that line, so the
    parent's wave survives until the parent's own clearing event."""
    _run({"session_id": "sess-o10", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    _run({"session_id": "sess-o10", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    assert _owners(home, "sess-o10") == {"parent", "agent:agent-aaa"}

    _run({"session_id": "sess-o10", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-aaa"}, home)
    assert _owners(home, "sess-o10") == {"parent"}, (
        "a helper must never clear a wave the parent is still blocked on")

    # The parent's own resolution then clears the last owner -> unlinked.
    _run({"session_id": "sess-o10", "hook_event_name": "PostToolUse",
          "tool_name": "Bash"}, home)
    assert not _flag_path(home, "sess-o10").exists()


def test_helper_wave_survives_parent_raising_and_resolving(home):
    """The mirror image (review round 2): a helper is blocked on its own
    prompt when the PARENT raises and resolves one of its own. The parent's
    clearing event removes only `parent`; the helper's wave survives until
    the helper's own clearing event."""
    _run({"session_id": "sess-o12", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    _run({"session_id": "sess-o12", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    assert _owners(home, "sess-o12") == {"parent", "agent:agent-aaa"}

    r = _run({"session_id": "sess-o12", "hook_event_name": "PostToolUse",
              "tool_name": "Bash"}, home)
    assert r.returncode == 0, r.stderr
    assert _owners(home, "sess-o12") == {"agent:agent-aaa"}, (
        "the parent resolving its own prompt must not clear a helper's wave")

    _run({"session_id": "sess-o12", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-aaa"}, home)
    assert not _flag_path(home, "sess-o12").exists()


def test_parent_pre_tool_use_adds_parent_owner_without_erasing_helper(home):
    """Every writer ADDS its own line -- the parent's AskUserQuestion/
    ExitPlanMode write included; it must not overwrite a pending helper."""
    _run({"session_id": "sess-o13", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    _run({"session_id": "sess-o13", "hook_event_name": "PreToolUse",
          "tool_name": "AskUserQuestion"}, home)
    lines = _flag_path(home, "sess-o13").read_text().splitlines()
    assert lines[0] == "AskUserQuestion"
    assert _owners(home, "sess-o13") == {"parent", "agent:agent-aaa"}


def test_legacy_flag_without_owner_lines_is_parent_owned(home):
    """A flag written by an older hook (type line only, no owner lines) is
    treated as `parent`-owned: a helper's clearing event leaves it alone, a
    helper adding its own line keeps the parent's share, and the parent's
    clearing event then removes only `parent`."""
    d = home / "claude_awaiting_input"
    d.mkdir(parents=True)
    (d / "sess-o14").write_text("permission_prompt")

    _run({"session_id": "sess-o14", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-aaa"}, home)
    assert _flag_path(home, "sess-o14").exists()

    _run({"session_id": "sess-o14", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    assert _owners(home, "sess-o14") == {"parent", "agent:agent-aaa"}

    _run({"session_id": "sess-o14", "hook_event_name": "Stop"}, home)
    assert _owners(home, "sess-o14") == {"agent:agent-aaa"}


def test_legacy_flag_is_cleared_by_parent(home):
    d = home / "claude_awaiting_input"
    d.mkdir(parents=True)
    (d / "sess-o15").write_text("permission_prompt")
    _run({"session_id": "sess-o15", "hook_event_name": "PostToolUse",
          "tool_name": "Bash"}, home)
    assert not _flag_path(home, "sess-o15").exists()


def test_session_end_clears_every_owner(home):
    """SessionEnd means the whole session -- helpers included -- is gone, so
    no owner can ever report back: the flag is removed outright rather than
    left for the 2h stale sweep."""
    _run({"session_id": "sess-o16", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    _run({"session_id": "sess-o16", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    r = _run({"session_id": "sess-o16", "hook_event_name": "SessionEnd"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-o16").exists()


def test_partial_clear_keeps_the_flags_mtime(home):
    """Removing one owner while others remain must not make the wave look
    freshly raised: focus picks the freshest wave by mtime and the watcher's
    self-heal / stale sweep age it by mtime."""
    _run({"session_id": "sess-o17", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    _run({"session_id": "sess-o17", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-bbb"}, home)
    fp = _flag_path(home, "sess-o17")
    old = fp.stat().st_mtime - 500
    os.utime(fp, (old, old))
    _run({"session_id": "sess-o17", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-aaa"}, home)
    assert _owners(home, "sess-o17") == {"agent:agent-bbb"}
    assert abs(fp.stat().st_mtime - old) < 1


def test_lock_file_is_a_dotfile_invisible_to_the_watcher(home):
    """The flock lives in the flag dir; it must be dot-prefixed so the
    watcher's scan (which skips dotfiles) never reads it as a session."""
    _run({"session_id": "sess-o18", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    visible = [n for n in os.listdir(home / "claude_awaiting_input")
               if not n.startswith(".")]
    assert visible == ["sess-o18"]


# ── agent_id validation (review round 2, Claude #4: a non-string id crashed
# the clear path with an unhashable-type TypeError). Anything not matching the
# session-id charset is a helper with no usable id: exit 0, no owner line of
# its own, log BAD_AGENT_ID without the value.
_BAD_AGENT_IDS = [["agent-aaa"], "agent-aaa\nparent", "evil/../id", {"x": 1}, 7,
                  # Round 3 (review #7): PRESENT-but-falsy ids are helpers
                  # with no usable id too -- never main-thread events.
                  "", [], {}, 0]


@pytest.mark.parametrize("bad", _BAD_AGENT_IDS)
def test_bad_agent_id_notification_waves_as_parent_owned(home, bad):
    r = _run({"session_id": "sess-b1", "hook_event_name": "Notification",
              "notification_type": "permission_prompt", "agent_id": bad}, home)
    assert r.returncode == 0, r.stderr
    lines = _flag_path(home, "sess-b1").read_text().splitlines()
    assert lines[0] == "permission_prompt" and len(lines) == 2
    assert _owners(home, "sess-b1") == {"parent"}, (
        "a bad agent_id must still wave, owned parent-style, and must never "
        "inject its own (possibly multi-line) value into the flag")
    log_text = (home / "claude_hook.log").read_text()
    assert "BAD_AGENT_ID" in log_text
    assert "evil" not in log_text and "agent-aaa" not in log_text


@pytest.mark.parametrize("bad", _BAD_AGENT_IDS)
def test_bad_agent_id_clearing_event_never_crashes_or_clears(home, bad):
    _run({"session_id": "sess-b2", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    _run({"session_id": "sess-b2", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    r = _run({"session_id": "sess-b2", "hook_event_name": "PostToolUse",
              "tool_name": "Bash", "agent_id": bad}, home)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert _owners(home, "sess-b2") == {"parent", "agent:agent-aaa"}
    log_text = (home / "claude_hook.log").read_text()
    assert "PostToolUse sess-b2 BAD_AGENT_ID" in log_text
    assert "PostToolUse sess-b2 HELPER_SKIP" in log_text


def test_bad_agent_id_helper_prompt_is_cleared_by_the_parent(home):
    """The fallback's consequence: a bad-id helper's wave is parent-owned, so
    the parent's next clearing event removes it (it cannot stick forever)."""
    _run({"session_id": "sess-b3", "hook_event_name": "Notification",
          "notification_type": "permission_prompt", "agent_id": ["x"]}, home)
    _run({"session_id": "sess-b3", "hook_event_name": "Stop"}, home)
    assert not _flag_path(home, "sess-b3").exists()


# ── concurrency (review round 2 #8 / round 3 #5 + Codex P1) ──────────────
# Every child is the REAL hook module, loaded in its own interpreter, then
# parked on an explicit start barrier: it reports "ready" on one pipe and
# blocks reading a second pipe whose write end only the test holds. Closing
# that end releases every child at once (EOF) -- no sleep-based settling, so
# the race window is as wide as the machine allows. With `nolock=True` the
# module's _flag_lock is swapped for a no-op, to prove the test can actually
# see the lost updates the flock exists to prevent.
_RACE_CHILD = r"""
import contextlib, importlib.util, os, sys
script, ready_fd, go_fd, nolock = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "1")
spec = importlib.util.spec_from_file_location("claude_pet_hook", script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
if nolock:
    mod._flag_lock = contextlib.nullcontext
os.write(ready_fd, b"r")
os.close(ready_fd)
os.read(go_fd, 1)
sys.exit(mod.main())
"""


def _race(home: Path, payloads: list[dict], *, nolock: bool = False) -> None:
    env = dict(os.environ)
    env["SQUID_PET_HOME"] = str(home)
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    procs = []
    try:
        for payload in payloads:
            p = subprocess.Popen(
                [sys.executable, "-c", _RACE_CHILD, str(SCRIPT),
                 str(ready_w), str(go_r), "1" if nolock else "0"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True, env=env,
                pass_fds=(ready_w, go_r))
            assert p.stdin is not None
            p.stdin.write(json.dumps(payload))  # buffered until main() reads
            p.stdin.close()
            procs.append(p)
        os.close(ready_w)
        ready_w = -1
        got = 0
        while got < len(procs):  # wait until EVERY child is parked
            chunk = os.read(ready_r, 64)
            assert chunk, "a race child died before reaching the barrier"
            got += len(chunk)
        os.close(go_w)  # release them all at once
        go_w = -1
        for p in procs:
            err = p.stderr.read() if p.stderr else ""
            assert p.wait(timeout=30) == 0, err
            assert "Traceback" not in err, err
    finally:
        for fd in (ready_r, ready_w, go_r, go_w):
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
            if p.stderr:
                p.stderr.close()


def _seed_owner_flag(home: Path, sid: str, owners: list[str]) -> None:
    import time as _time
    d = home / "claude_awaiting_input"
    d.mkdir(parents=True, exist_ok=True)
    now = _time.time()
    (d / sid).write_text("permission_prompt\n"
                         + "\n".join(f"{o} {now:.6f}" for o in owners))


def _raise_payload(sid: str, agent_id: str | None = None) -> dict:
    """A wave-raising event for the races. Since round 4 only a Notification
    raises (PermissionRequest is attribution-only), so a helper's raise is a
    TAGGED Notification -- synthetic, but it exercises exactly the same
    locked owner add."""
    payload = {"session_id": sid, "hook_event_name": "Notification",
               "notification_type": "permission_prompt"}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _big_race_payloads(sid: str, seeded: list[str], added: list[str]) -> list[dict]:
    return (
        [{"session_id": sid, "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": a} for a in seeded]
        + [_raise_payload(sid, a) for a in added]
        + [_raise_payload(sid)]
        # Parent PermissionRequests race the rest through the pending store
        # (same lock); whether or not the untagged Notification pairs with
        # one, its owner is `parent`, so the expected set is unchanged.
        + [{"session_id": sid, "hook_event_name": "PermissionRequest",
            "tool_name": "Bash"} for _ in range(2)]
    )


def test_concurrent_hooks_leave_exactly_the_right_owner_set(home):
    """Review round 2: the read-modify-write of the shared owner file must be
    serialized. Seed five helper owners, then release 16 hook processes at
    once -- those five helpers resolving, ten new helpers raising, the parent
    raising -- and the survivors must be exactly the new set."""
    sid = "sess-cc"
    seeded = [f"agent-c{i}" for i in range(5)]
    added = [f"agent-a{i}" for i in range(10)]
    for _ in range(3):
        _seed_owner_flag(home, sid, [f"agent:{a}" for a in seeded])
        _race(home, _big_race_payloads(sid, seeded, added))
        assert _owners(home, sid) == {"parent", *(f"agent:{a}" for a in added)}
        assert _flag_path(home, sid).read_text().splitlines()[0] == "permission_prompt"


def test_last_owner_removal_racing_adds_keeps_every_add(home):
    """Round 3 (#8): the LAST remaining owner resolves while new owners are
    being added -- the delete-then-recreate path. Whatever the interleaving,
    the unlink must never swallow an add that landed first, and a recreate
    must never resurrect the removed owner."""
    sid = "sess-cl"
    added = [f"agent-n{i}" for i in range(6)]
    payloads = (
        [{"session_id": sid, "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-last"}]
        + [_raise_payload(sid, a) for a in added]
        + [_raise_payload(sid)]
    )
    for _ in range(3):
        _seed_owner_flag(home, sid, ["agent:agent-last"])
        _race(home, payloads)
        assert _owners(home, sid) == {"parent", *(f"agent:{a}" for a in added)}


def test_the_concurrency_test_fails_without_the_lock(home):
    """Proves the race harness above has teeth: with _flag_lock swapped for a
    no-op, the same 16-process race must lose or resurrect an owner at least
    once in 20 tries (it stops at the first failure)."""
    import time as _time
    sid = "sess-nl"
    seeded = [f"agent-c{i}" for i in range(5)]
    added = [f"agent-a{i}" for i in range(10)]
    expected = {"parent", *(f"agent:{a}" for a in added)}
    deadline = _time.monotonic() + 25
    for attempt in range(20):
        _seed_owner_flag(home, sid, [f"agent:{a}" for a in seeded])
        _race(home, _big_race_payloads(sid, seeded, added), nolock=True)
        if _owners(home, sid) != expected:
            return
        if _time.monotonic() > deadline:
            break
    pytest.fail(f"no lost update in {attempt + 1} unlocked races -- the "
                "concurrency tests cannot tell a missing lock from a working one")


# ── Codex round 2 P1: no lock-free "no flag" fast path on removal ─────────
# A removal that saw "no flag" and returned WITHOUT taking the lock could
# overtake an add that was already in flight (holding the lock, about to
# publish), leaving that add's wave behind with nobody left to clear it. Every
# removal must now take the flock and only then look for the file. Made
# deterministic by holding the lock in the test (standing in for the in-flight
# add), spying on flock to know the removal has reached it, and only then
# publishing the flag.
@pytest.mark.parametrize("which", ["owner", "entire"])
def test_removal_waits_for_an_in_flight_add_instead_of_racing_past_it(
        tmp_path, monkeypatch, which):
    import fcntl
    import threading
    import time as _time

    mod = _load_hook_module(tmp_path)
    os.makedirs(mod.FLAG_DIR)
    flag = Path(mod.FLAG_DIR) / "sess-race"
    holder = open(os.path.join(mod.FLAG_DIR, ".lock"), "a")
    fcntl.flock(holder, fcntl.LOCK_EX)  # the in-flight add holds the lock
    reached = threading.Event()
    real_flock = fcntl.flock

    def spy(fd, op):
        if threading.current_thread() is not threading.main_thread():
            reached.set()
        return real_flock(fd, op)
    monkeypatch.setattr(mod.fcntl, "flock", spy)

    result: list = []

    def remove():
        if which == "owner":
            result.append(mod._remove_flag_owner("sess-race", "parent"))
        else:
            result.append(mod._remove_flag_entirely("sess-race"))
    t = threading.Thread(target=remove)
    t.start()
    try:
        assert reached.wait(5), (
            "the removal returned without ever taking the lock (lock-free "
            f"fast path), result={result!r}")
        assert t.is_alive()
        # The add publishes while still holding the lock, then releases it.
        flag.write_text(f"permission_prompt\nparent {_time.time():.6f}")
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
        t.join(5)
    assert result == (["REMOVED"] if which == "owner" else [True])
    assert not flag.exists(), "the in-flight add's wave was left behind"


def test_helper_clear_with_no_flag_dir_is_a_quiet_noop(home):
    """The removal now always takes the lock, which lives IN the flag dir --
    a helper's clearing event on a machine that never raised a wave must
    still exit 0 cleanly (no HELPER_CLEAR_FAILED)."""
    r = _run({"session_id": "sess-nd", "hook_event_name": "PostToolUse",
              "tool_name": "Bash", "agent_id": "agent-aaa"}, home)
    assert r.returncode == 0, r.stderr
    log_text = (home / "claude_hook.log").read_text()
    assert "FAILED" not in log_text
    assert "PostToolUse sess-nd HELPER_SKIP" in log_text


def test_owner_pre_tool_use_does_not_clear_its_own_prompt(home):
    """Only an event that would normally CLEAR the flag (PostToolUse, Stop,
    ... -- or SubagentStop, the helper's own Stop) may clear an owned flag.
    PreToolUse normally WRITES the flag, never clears it, so the owner's
    PreToolUse (e.g. AskUserQuestion -- blocked on the user again) must
    leave its own pending wave alone."""
    _run({"session_id": "sess-o11", "hook_event_name": "Notification",
          "notification_type": "permission_prompt",
          "agent_id": "agent-aaa"}, home)
    r = _run({"session_id": "sess-o11", "hook_event_name": "PreToolUse",
              "tool_name": "AskUserQuestion", "agent_id": "agent-aaa"}, home)
    assert r.returncode == 0, r.stderr
    assert _flag_path(home, "sess-o11").exists()
    log_text = (home / "claude_hook.log").read_text()
    assert "PreToolUse sess-o11 HELPER_SKIP (agent_id)" in log_text


def test_multiple_stop_sessions_are_independent(home):
    _run({"session_id": "sess-A", "hook_event_name": "Stop"}, home)
    _run({"session_id": "sess-B", "hook_event_name": "Stop"}, home)
    assert _finished_path(home, "sess-A").exists()
    assert _finished_path(home, "sess-B").exists()


# ── PreCompact / PostCompact (Pink-2026-08-30: Pink noticed Squid
# flashing to a generic "thinking" during a context compaction with no
# indication why, and asked for it called out by name) ─────────────────
def test_precompact_writes_recap_flag(home):
    r = _run({"session_id": "sess-c1", "hook_event_name": "PreCompact",
              "trigger": "manual", "custom_instructions": ""}, home)
    assert r.returncode == 0, r.stderr
    fp = _recap_path(home, "sess-c1")
    assert fp.exists()
    assert fp.read_text() == "manual"


def test_precompact_auto_trigger_recorded(home):
    r = _run({"session_id": "sess-c2", "hook_event_name": "PreCompact",
              "trigger": "auto"}, home)
    assert r.returncode == 0, r.stderr
    assert _recap_path(home, "sess-c2").read_text() == "auto"


def test_postcompact_removes_recap_flag(home):
    _run({"session_id": "sess-c3", "hook_event_name": "PreCompact",
          "trigger": "manual"}, home)
    assert _recap_path(home, "sess-c3").exists()

    r = _run({"session_id": "sess-c3", "hook_event_name": "PostCompact"}, home)
    assert r.returncode == 0, r.stderr
    assert not _recap_path(home, "sess-c3").exists()


def test_postcompact_on_nonexistent_flag_is_a_noop(home):
    r = _run({"session_id": "never-recapped",
              "hook_event_name": "PostCompact"}, home)
    assert r.returncode == 0, r.stderr


def test_precompact_does_not_touch_other_flags(home):
    """PreCompact/PostCompact are independent signals in their own
    directory -- must not create or remove anything in
    claude_awaiting_input/ or claude_finished/."""
    r = _run({"session_id": "sess-c4", "hook_event_name": "PreCompact",
              "trigger": "auto"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-c4").exists()
    assert not _finished_path(home, "sess-c4").exists()


def test_session_end_also_clears_a_stuck_recap_flag(home):
    """Crash-safety: a session ending mid-compact (PostCompact never
    fires) must not leave Squid waving "recapping" forever."""
    _run({"session_id": "sess-c5", "hook_event_name": "PreCompact",
          "trigger": "manual"}, home)
    assert _recap_path(home, "sess-c5").exists()

    r = _run({"session_id": "sess-c5", "hook_event_name": "SessionEnd"}, home)
    assert r.returncode == 0, r.stderr
    assert not _recap_path(home, "sess-c5").exists()


def test_user_prompt_submit_on_nonexistent_flag_is_a_noop(home):
    r = _run({"session_id": "never-had-a-flag",
              "hook_event_name": "UserPromptSubmit"}, home)
    assert r.returncode == 0, r.stderr


def test_multiple_sessions_are_independent(home):
    _run({"session_id": "sess-A", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    _run({"session_id": "sess-B", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    assert _flag_path(home, "sess-A").exists()
    assert _flag_path(home, "sess-B").exists()

    _run({"session_id": "sess-A", "hook_event_name": "UserPromptSubmit"}, home)
    assert not _flag_path(home, "sess-A").exists()
    assert _flag_path(home, "sess-B").exists(), \
        "removing sess-A's flag must not touch sess-B's"


def test_malformed_json_does_not_crash(home):
    r = _run(None, home)  # empty stdin handled separately below
    assert r.returncode == 0

    env = dict(os.environ)
    env["SQUID_PET_HOME"] = str(home)
    r2 = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="not valid json {{{", capture_output=True, text=True,
        timeout=10, env=env,
    )
    assert r2.returncode == 0, r2.stderr


def test_missing_session_id_does_not_crash(home):
    r = _run({"hook_event_name": "Notification",
              "notification_type": "permission_prompt"}, home)
    assert r.returncode == 0, r.stderr
    # No flag directory content should be produced at all.
    flag_dir = home / "claude_awaiting_input"
    assert not flag_dir.exists() or list(flag_dir.iterdir()) == []


def test_unknown_hook_event_does_not_crash(home):
    r = _run({"session_id": "sess-6", "hook_event_name": "SomeFutureEvent"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-6").exists()


def test_log_file_is_written(home):
    _run({"session_id": "sess-7", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    log = home / "claude_hook.log"
    assert log.exists()
    assert "sess-7" in log.read_text()


def test_log_file_is_truncated_when_large(home):
    """The log must not grow unbounded over months of real usage."""
    home.mkdir(parents=True, exist_ok=True)
    log = home / "claude_hook.log"
    # Pre-seed a log well past the truncation threshold.
    with open(log, "w") as f:
        for i in range(20_000):
            f.write(f"1700000000 filler line {i}\n")
    size_before = log.stat().st_size
    assert size_before > 200_000

    r = _run({"session_id": "sess-8", "hook_event_name": "Notification",
              "notification_type": "permission_prompt"}, home)
    assert r.returncode == 0, r.stderr

    size_after = log.stat().st_size
    assert size_after < size_before, "log should have been truncated"
    content = log.read_text()
    assert "sess-8" in content, "the triggering event must survive truncation"


def test_defaults_to_real_home_when_env_unset(tmp_path, monkeypatch):
    """Without SQUID_PET_HOME set, the script must fall back to
    ~/.squid-pet -- verified by checking the script's own default
    resolves relative to HOME, not by actually touching the real
    directory (that would pollute the developer's machine)."""
    env = dict(os.environ)
    env.pop("SQUID_PET_HOME", None)
    env["HOME"] = str(tmp_path)  # redirect HOME itself instead
    r = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"session_id": "sess-9", "hook_event_name": "Notification",
                          "notification_type": "permission_prompt"}),
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".squid-pet" / "claude_awaiting_input" / "sess-9").exists()


# ── Turn-in-flight signal (Pink-2026-09-01) ────────────────────────────
# ClaudeCodeDetector infers "thinking" from transcript mtime
# (STREAMING_STALE_SEC), but Claude Code only writes a transcript entry
# when a block COMPLETES. An extended thinking stretch writes nothing, so
# past the staleness window Squid decided nobody was home and showed idle
# while the UI said "thinking some more" -- measured at up to 29s of
# wrongly-idle in a single stretch. UserPromptSubmit/Stop bracket a turn
# exactly, with no inference and no transcript content read.
def test_user_prompt_submit_marks_the_turn_active(home):
    r = _run({"session_id": "sess-t1", "hook_event_name": "UserPromptSubmit"}, home)
    assert r.returncode == 0, r.stderr
    assert _turn_active_path(home, "sess-t1").exists()


def test_stop_ends_the_active_turn(home):
    _run({"session_id": "sess-t2", "hook_event_name": "UserPromptSubmit"}, home)
    assert _turn_active_path(home, "sess-t2").exists()

    r = _run({"session_id": "sess-t2", "hook_event_name": "Stop"}, home)
    assert r.returncode == 0, r.stderr
    assert not _turn_active_path(home, "sess-t2").exists()


def test_session_end_ends_the_active_turn(home):
    """Crash safety: a session killed mid-turn must not leave Squid
    thinking forever."""
    _run({"session_id": "sess-t3", "hook_event_name": "UserPromptSubmit"}, home)
    r = _run({"session_id": "sess-t3", "hook_event_name": "SessionEnd"}, home)
    assert r.returncode == 0, r.stderr
    assert not _turn_active_path(home, "sess-t3").exists()


def test_turn_active_sessions_are_independent(home):
    _run({"session_id": "sess-t4", "hook_event_name": "UserPromptSubmit"}, home)
    _run({"session_id": "sess-t5", "hook_event_name": "UserPromptSubmit"}, home)
    _run({"session_id": "sess-t4", "hook_event_name": "Stop"}, home)

    assert not _turn_active_path(home, "sess-t4").exists()
    assert _turn_active_path(home, "sess-t5").exists(), (
        "one session finishing must not end another's turn")


def test_turn_active_flag_holds_no_prompt_content(home):
    """Same content-blind contract as every other flag here: session_id and
    mtime only, never what the user actually typed."""
    _run({"session_id": "sess-t6", "hook_event_name": "UserPromptSubmit",
          "prompt": "something private the user typed"}, home)
    assert "private" not in _turn_active_path(home, "sess-t6").read_text()


# ── StopFailure (Pink-2026-09-16: the real "turn failed on an API error"
# signal that drives the concerned/warning sprite -- see
# watcher.claude_freshest_failure()). Claude Code fires StopFailure, NOT
# Stop, when a turn ends due to an API error (usage/rate limit, overload,
# auth, billing, ...), carrying a machine-readable error_type. This is the
# only inference-free way to know she is blocked by an error rather than
# just quiet. ────────────────────────────────────────────────────────────
def test_stop_failure_writes_failed_flag_with_error_type(home):
    r = _run({"session_id": "sess-f1", "hook_event_name": "StopFailure",
              "error_type": "rate_limit"}, home)
    assert r.returncode == 0, r.stderr
    fp = _failed_path(home, "sess-f1")
    assert fp.exists()
    assert fp.read_text() == "rate_limit"


def test_stop_failure_missing_error_type_defaults_unknown(home):
    """A StopFailure with no error_type still means the turn failed -- record
    it as 'unknown' rather than dropping the signal."""
    r = _run({"session_id": "sess-f2", "hook_event_name": "StopFailure"}, home)
    assert r.returncode == 0, r.stderr
    assert _failed_path(home, "sess-f2").read_text() == "unknown"


def test_stop_failure_closes_the_turn_bracket(home):
    """A failed turn is an ended turn: StopFailure must clear claude_turn_active
    so the stall/thinking path can't keep painting 'thinking' over the error."""
    _run({"session_id": "sess-f3", "hook_event_name": "UserPromptSubmit"}, home)
    assert _turn_active_path(home, "sess-f3").exists()

    r = _run({"session_id": "sess-f3", "hook_event_name": "StopFailure",
              "error_type": "overloaded"}, home)
    assert r.returncode == 0, r.stderr
    assert not _turn_active_path(home, "sess-f3").exists()


def test_stop_failure_clears_a_stuck_awaiting_input_flag(home):
    """Pink-2026-09-24 (review #4): a turn that fails on an API error while a
    permission prompt is up must clear the wave. Without this, 'your turn' kept
    waving until the 2h sweep and -- approval taking prime over concerned --
    masked the concerned sprite the whole time."""
    _run({"session_id": "sess-f9", "hook_event_name": "Notification",
          "notification_type": "permission_prompt"}, home)
    assert _flag_path(home, "sess-f9").exists()

    r = _run({"session_id": "sess-f9", "hook_event_name": "StopFailure",
              "error_type": "rate_limit"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-f9").exists()
    # The failure signal itself must still land -- clearing is additive.
    assert _failed_path(home, "sess-f9").read_text() == "rate_limit"


def test_stop_failure_closes_the_turn_even_when_failed_dir_is_unwritable(home):
    """Pink-2026-09-24 (review #9): the failed-flag write must not gate the
    turn-close. If claude_failed/ can't be created, the turn still ENDED --
    turn_in_flight must not stick, or the stall path keeps painting 'thinking'
    over the error."""
    _run({"session_id": "sess-f10", "hook_event_name": "UserPromptSubmit"}, home)
    assert _turn_active_path(home, "sess-f10").exists()

    # A regular file sits where claude_failed/ would go, so os.makedirs raises.
    (home / "claude_failed").write_text("not a directory")

    r = _run({"session_id": "sess-f10", "hook_event_name": "StopFailure",
              "error_type": "overloaded"}, home)
    assert r.returncode == 0, r.stderr
    assert not _turn_active_path(home, "sess-f10").exists(), (
        "turn stayed in flight after a StopFailure whose flag write failed")
    assert (home / "claude_failed").is_file()  # untouched, and no crash


def test_stop_failure_does_not_write_finished_flag(home):
    """A failed turn is not a completion -- it must never look like a Stop and
    trigger the celebrate/groove path."""
    r = _run({"session_id": "sess-f4", "hook_event_name": "StopFailure",
              "error_type": "server_error"}, home)
    assert r.returncode == 0, r.stderr
    assert not _finished_path(home, "sess-f4").exists()


def test_stop_failure_is_content_blind(home):
    """Only the error_type CATEGORY is recorded, never any message text --
    same privacy contract as every other flag here."""
    r = _run({"session_id": "sess-f5", "hook_event_name": "StopFailure",
              "error_type": "rate_limit",
              "last_assistant_message": "some potentially sensitive text",
              "message": "another sensitive detail"}, home)
    assert r.returncode == 0, r.stderr
    body = _failed_path(home, "sess-f5").read_text()
    assert "sensitive" not in body
    assert body == "rate_limit"


def test_user_prompt_submit_clears_failed_flag(home):
    """Retrying (a new prompt) means the user has reacted to the error -- the
    concerned state should stand down."""
    _run({"session_id": "sess-f6", "hook_event_name": "StopFailure",
          "error_type": "rate_limit"}, home)
    assert _failed_path(home, "sess-f6").exists()

    r = _run({"session_id": "sess-f6", "hook_event_name": "UserPromptSubmit"}, home)
    assert r.returncode == 0, r.stderr
    assert not _failed_path(home, "sess-f6").exists()


def test_session_end_clears_failed_flag(home):
    """Crash safety: a session that ends must not leave a stuck failure flag."""
    _run({"session_id": "sess-f7", "hook_event_name": "StopFailure",
          "error_type": "billing_error"}, home)
    assert _failed_path(home, "sess-f7").exists()

    r = _run({"session_id": "sess-f7", "hook_event_name": "SessionEnd"}, home)
    assert r.returncode == 0, r.stderr
    assert not _failed_path(home, "sess-f7").exists()


def test_stop_failure_is_not_logged_as_unknown_event(home):
    """StopFailure is a HANDLED event -- it must not also appear as
    UNKNOWN_EVENT in claude_hook.log, which is exactly the log someone reads
    when a concerned flag looks stuck."""
    _run({"session_id": "sess-f8", "hook_event_name": "StopFailure",
          "error_type": "rate_limit"}, home)
    log = (home / "claude_hook.log").read_text()
    assert "StopFailure sess-f8 WRITE" in log
    assert "UNKNOWN_EVENT" not in log


# ── session -> tty registry (Pink-2026-09-16 wrong-window fix) ──────────
# The hook records the session's controlling terminal so "take me there"
# can raise the exact tab/app that fired a signal, instead of guessing by
# cwd (ambiguous when two sessions share a directory). The recording itself
# depends on the process having a controlling tty, which a pytest subprocess
# may not -- so recording is unit-tested by importing the module, while the
# SessionEnd cleanup (which must run regardless) is tested end-to-end.
import importlib.util as _ilu
import tempfile as _tempfile

_FALLBACK_HOME: Path | None = None


def _shared_fallback_home() -> Path:
    """One throwaway SQUID_PET_HOME for the whole run, for the handful of
    unit tests that take no tmp_path. Shared rather than per-call: mkdtemp on
    every _load_hook_module() left an orphan dir under /var/folders for each
    of the ~18 call sites, every run, and they are genuinely written to (the
    hook binds LOG_PATH inside them). Python removes it at exit."""
    global _FALLBACK_HOME
    if _FALLBACK_HOME is None:
        _FALLBACK_HOME = Path(
            _tempfile.TemporaryDirectory(prefix="squid-hook-test-").name)
        _FALLBACK_HOME.mkdir(parents=True, exist_ok=True)
    return _FALLBACK_HOME


def _load_hook_module(tmp_path: Path | None = None):
    """Import the hook script for unit-level tests.

    SQUID_PET_HOME is pointed at a throwaway dir BEFORE exec_module, because
    the script binds LOG_PATH (and the flag dirs) at import time. Without it
    any test that reaches a `_log(...)` call appends to the developer's real
    ~/.squid-pet/claude_hook.log -- a file the running watcher reads -- which
    contradicts this module's own docstring. Verified: an earlier revision of
    the write-failure test wrote 13 lines into the real log.
    """
    home = tmp_path if tmp_path is not None else _shared_fallback_home()
    prev = os.environ.get("SQUID_PET_HOME")
    os.environ["SQUID_PET_HOME"] = str(home)
    try:
        spec = _ilu.spec_from_file_location("claude_pet_hook", SCRIPT)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if prev is None:
            os.environ.pop("SQUID_PET_HOME", None)
        else:
            os.environ["SQUID_PET_HOME"] = prev
    return mod


def _tty_path(home: Path, session_id: str) -> Path:
    return home / "claude_session_tty" / session_id


def test_ancestor_walk_finds_the_claude_tty_when_the_hook_has_none():
    """The real Claude Code runtime: the hook process has no controlling
    terminal (tty ??), but its `claude` parent does. The walk must climb
    past the terminal-less hook to the ancestor that owns the tty -- this
    is what makes the whole fix work in production, where the hook's own
    /dev/tty is unavailable."""
    mod = _load_hook_module()
    # pid 500 = hook (no tty), 400 = claude (ttys008), 300 = login shell
    ps_output = (
        "500 400 ??\n"
        "400 300 ttys008\n"
        "300   1 ttys008\n"
        "999   1 ttys000\n"   # an unrelated session -- must be ignored
    )
    assert mod._first_ancestor_tty(ps_output, 500) == "/dev/ttys008"


def test_ancestor_walk_returns_none_when_no_ancestor_has_a_tty():
    """A truly headless run (no terminal anywhere up the chain) yields None,
    and the reader falls back to the cwd guess."""
    mod = _load_hook_module()
    ps_output = "500 400 ??\n400 300 ??\n300 1 ??\n"
    assert mod._first_ancestor_tty(ps_output, 500) is None


def test_records_the_controlling_tty_when_there_is_one(tmp_path, monkeypatch):
    mod = _load_hook_module()
    monkeypatch.setattr(mod, "TTY_DIR", str(tmp_path / "tty"))
    monkeypatch.setattr(mod, "_controlling_tty", lambda: "/dev/ttys008")
    mod._record_session_tty("sess-cursor", "UserPromptSubmit")
    assert (tmp_path / "tty" / "sess-cursor").read_text() == "/dev/ttys008"


def test_records_nothing_when_there_is_no_tty(tmp_path, monkeypatch):
    """A detached/headless spawn has no controlling terminal -- leave no
    entry (the reader falls back to cwd) rather than crashing the hook."""
    mod = _load_hook_module()
    monkeypatch.setattr(mod, "TTY_DIR", str(tmp_path / "tty"))
    monkeypatch.setattr(mod, "_controlling_tty", lambda: None)
    mod._record_session_tty("sess-x", "UserPromptSubmit")
    assert not (tmp_path / "tty" / "sess-x").exists()


def test_session_end_clears_the_tty_registry_entry(home):
    """A recorded tty must not outlive its session (it would misdirect a
    later same-tty session). SessionEnd removes it; it runs whether or not
    the ending process still has a tty."""
    (home / "claude_session_tty").mkdir(parents=True)
    _tty_path(home, "sess-gone").write_text("/dev/ttys008")

    r = _run({"session_id": "sess-gone", "hook_event_name": "SessionEnd"}, home)
    assert r.returncode == 0, r.stderr
    assert not _tty_path(home, "sess-gone").exists()



# ─────────────────────────────────────────────────────────────────────────
# Pink-2026-09-19: _record_session_tty used to re-resolve on EVERY event.
# Hooks have no controlling terminal, so that meant forking `ps -Ao` over
# every process on the machine (73-140ms measured) after every tool call,
# on a path that blocks the turn -- for a value that had not changed.
# These pin the new rule: resolve ONLY on turn-open (a resumed session can
# move tabs, and it cannot do so without submitting a prompt). Nothing else
# resolves -- not even to fill a missing entry, or a session with no terminal
# anywhere would keep paying the full cost on every tool call.
# ─────────────────────────────────────────────────────────────────────────
def _counting_tty(value):
    """Stand-in for _controlling_tty that records how often it ran, which is
    the thing actually being optimised (each call is the expensive `ps`)."""
    calls = []

    def _fn():
        calls.append(1)
        return value
    return _fn, calls


def test_non_turn_open_events_never_resolve_the_tty(tmp_path, monkeypatch):
    """The hot path: PostToolUse fires after every tool call, and hooks block
    the turn. It must not run the expensive lookup at all -- not even to fill
    a MISSING entry, or a session with no terminal anywhere (`claude -p`, CI)
    would keep paying the full `ps` cost on every tool call."""
    mod = _load_hook_module()
    tty_dir = tmp_path / "tty"
    monkeypatch.setattr(mod, "TTY_DIR", str(tty_dir))
    fn, calls = _counting_tty("/dev/ttys999")
    monkeypatch.setattr(mod, "_controlling_tty", fn)

    for event in ("PostToolUse", "PreToolUse", "Notification", "Stop", "StopFailure"):
        mod._record_session_tty("sess-a", event)

    assert calls == [], "the expensive tty lookup ran off the turn-open path"


def test_an_existing_entry_survives_the_hot_path(tmp_path, monkeypatch):
    mod = _load_hook_module()
    tty_dir = tmp_path / "tty"
    tty_dir.mkdir()
    (tty_dir / "sess-a").write_text("/dev/ttys003")
    monkeypatch.setattr(mod, "TTY_DIR", str(tty_dir))
    fn, _ = _counting_tty("/dev/ttys999")
    monkeypatch.setattr(mod, "_controlling_tty", fn)

    mod._record_session_tty("sess-a", "PostToolUse")

    assert (tty_dir / "sess-a").read_text() == "/dev/ttys003"


def test_an_existing_entry_is_touched_on_the_hot_path(tmp_path, monkeypatch):
    """Pink-2026-09-24 (review #2): a live session's entry must not age out of
    the watcher's 2h stale sweep during a long turn with no new prompt. Every
    non-turn-open event cheaply bumps its mtime -- no `ps`, no write, no new
    entry."""
    import time as _time

    mod = _load_hook_module()
    tty_dir = tmp_path / "tty"
    tty_dir.mkdir()
    entry = tty_dir / "sess-a"
    entry.write_text("/dev/ttys003")
    old = _time.time() - 10_000
    os.utime(entry, (old, old))
    monkeypatch.setattr(mod, "TTY_DIR", str(tty_dir))
    fn, calls = _counting_tty("/dev/ttys999")
    monkeypatch.setattr(mod, "_controlling_tty", fn)

    mod._record_session_tty("sess-a", "PostToolUse")

    assert calls == [], "the expensive ps lookup ran on the hot path"
    assert entry.read_text() == "/dev/ttys003", "content must be unchanged"
    assert entry.stat().st_mtime > old, "mtime must be bumped so the sweep keeps it"


def test_the_touch_never_creates_a_missing_entry(tmp_path, monkeypatch):
    """The touch is total but must never fabricate an entry: a session with no
    recorded tty stays unrecorded until its next turn-open (one turn of
    cwd-guess fallback), never a bare zero-byte file."""
    mod = _load_hook_module()
    tty_dir = tmp_path / "tty"
    tty_dir.mkdir()
    monkeypatch.setattr(mod, "TTY_DIR", str(tty_dir))
    fn, calls = _counting_tty("/dev/ttys999")
    monkeypatch.setattr(mod, "_controlling_tty", fn)

    mod._record_session_tty("sess-missing", "PostToolUse")

    assert not (tty_dir / "sess-missing").exists()
    assert calls == []


def test_reresolves_on_turn_open_so_a_resumed_session_can_move_tabs(tmp_path, monkeypatch):
    """`claude --resume` in another tab keeps the session_id but changes the
    tty. A resumed session cannot do anything without submitting a prompt, so
    turn-open is both the only place the tty can have changed and the only
    place we need to look."""
    mod = _load_hook_module()
    tty_dir = tmp_path / "tty"
    tty_dir.mkdir()
    (tty_dir / "sess-a").write_text("/dev/ttys003")     # where it used to live
    monkeypatch.setattr(mod, "TTY_DIR", str(tty_dir))
    fn, calls = _counting_tty("/dev/ttys007")           # resumed in a new tab
    monkeypatch.setattr(mod, "_controlling_tty", fn)

    mod._record_session_tty("sess-a", "UserPromptSubmit")

    assert len(calls) == 1
    assert (tty_dir / "sess-a").read_text() == "/dev/ttys007"


def test_a_missing_entry_is_filled_at_the_next_turn_open(tmp_path, monkeypatch):
    """A hook installed mid-session never saw that session's earlier prompts.
    It gets an entry at the next one -- one turn of cwd-guess fallback."""
    mod = _load_hook_module()
    tty_dir = tmp_path / "tty"
    monkeypatch.setattr(mod, "TTY_DIR", str(tty_dir))
    fn, calls = _counting_tty("/dev/ttys004")
    monkeypatch.setattr(mod, "_controlling_tty", fn)

    mod._record_session_tty("sess-new", "PostToolUse")
    assert not (tty_dir / "sess-new").exists()
    assert calls == []

    mod._record_session_tty("sess-new", "UserPromptSubmit")
    assert (tty_dir / "sess-new").read_text() == "/dev/ttys004"


def test_a_turn_open_lookup_that_finds_no_terminal_drops_the_stale_entry(
        tmp_path, monkeypatch):
    """The lookup RAN and established there is no controlling terminal -- a
    fact. Turn-open is the only refresh point, so keeping the old value would
    strand the whole turn on the PREVIOUS tab after a resume, and a
    confidently wrong tty is worse than none (no entry merely falls back to
    the cwd guess).

    Contrast test_a_transient_lookup_failure_keeps_the_existing_entry: when
    the lookup could not run at all (`ps` failed or timed out, flagged by
    _TTY_LOOKUP_FAILED) the entry is KEPT. These two inputs look identical to
    _controlling_tty -- both give None -- which is exactly why the flag
    exists. The stub here leaves it unset, i.e. a clean lookup."""
    mod = _load_hook_module()
    tty_dir = tmp_path / "tty"
    tty_dir.mkdir()
    (tty_dir / "sess-a").write_text("/dev/ttys003")     # the OLD tab
    monkeypatch.setattr(mod, "TTY_DIR", str(tty_dir))
    monkeypatch.setattr(mod, "_TTY_LOOKUP_FAILED", False)
    fail, _ = _counting_tty(None)
    monkeypatch.setattr(mod, "_controlling_tty", fail)

    mod._record_session_tty("sess-a", "UserPromptSubmit")

    assert not (tty_dir / "sess-a").exists(), "a stale tty survived a failed refresh"


def test_the_write_is_atomic_and_leaves_no_orphan_tmp(tmp_path, monkeypatch):
    """tmp+rename, with the pid in the tmp name so concurrent hooks for the
    same session cannot truncate each other's file and publish a zero-byte
    entry. A failed write must not leave the tmp behind -- nothing sweeps
    TTY_DIR."""
    mod = _load_hook_module()
    tty_dir = tmp_path / "tty"
    monkeypatch.setattr(mod, "TTY_DIR", str(tty_dir))
    fn, _ = _counting_tty("/dev/ttys004")
    monkeypatch.setattr(mod, "_controlling_tty", fn)

    mod._record_session_tty("sess-a", "UserPromptSubmit")

    assert (tty_dir / "sess-a").read_text() == "/dev/ttys004"
    assert list(tty_dir.glob("*.tmp")) == [], "left an orphan tmp file"


def test_a_transient_lookup_failure_keeps_the_existing_entry(tmp_path, monkeypatch):
    """"Could not find out" is NOT "there is no terminal".

    _controlling_tty() returns None for both, but `ps` timing out on a loaded
    machine must not destroy a valid entry -- turn-open is the only refresh
    point, so the whole turn would fall back to the cwd guess, which cannot
    tell apart two sessions sharing a directory in different hosts. That is
    the ambiguity this registry exists to resolve.
    """
    mod = _load_hook_module()
    tty_dir = tmp_path / "tty"
    tty_dir.mkdir()
    (tty_dir / "sess-a").write_text("/dev/ttys003")
    monkeypatch.setattr(mod, "TTY_DIR", str(tty_dir))
    monkeypatch.setattr(mod, "_controlling_tty", lambda: None)
    monkeypatch.setattr(mod, "_TTY_LOOKUP_FAILED", True)     # ps blew up

    mod._record_session_tty("sess-a", "UserPromptSubmit")

    assert (tty_dir / "sess-a").read_text() == "/dev/ttys003", (
        "a transient ps failure destroyed a valid registry entry"
    )


def test_ps_failure_is_recorded_as_a_failure_not_as_no_terminal(monkeypatch):
    """The flag that distinction depends on is actually set when `ps` blows
    up -- otherwise the guard above is inert and the entry gets destroyed."""
    mod = _load_hook_module()

    def _boom(*args, **kwargs):
        raise OSError("ps exploded")
    monkeypatch.setattr("subprocess.run", _boom)

    assert mod._ancestor_tty_via_ps() is None
    assert mod._TTY_LOOKUP_FAILED is True


def test_a_clean_ps_run_that_finds_nothing_is_not_a_failure(monkeypatch):
    """The other half: `ps` ran fine and no ancestor has a terminal. That is a
    fact, so the flag must stay false and the stale entry may be dropped."""
    mod = _load_hook_module()
    monkeypatch.setattr(mod, "_TTY_LOOKUP_FAILED", False)

    monkeypatch.setattr(os, "getpid", lambda: 42)

    class _Ok:
        returncode = 0
        # A complete snapshot that DOES contain us -- we walked to the top and
        # genuinely found no terminal. (Our own pid must be present, or the
        # table is partial and that is a failed lookup instead: see
        # test_a_snapshot_missing_our_own_pid_is_a_failure.)
        stdout = "42 1 ??\n1 0 ??\n"
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Ok())

    assert mod._ancestor_tty_via_ps() is None
    assert mod._TTY_LOOKUP_FAILED is False


def test_a_ps_that_exits_nonzero_is_a_failure_not_no_terminal(monkeypatch):
    """`ps` can RUN and still tell us nothing -- sandboxed/seatbelt env, a
    broken `ps` on PATH. Empty stdout must not be read as "no ancestor has a
    terminal", or the caller deletes a good registry entry on the strength of
    an empty snapshot."""
    mod = _load_hook_module()
    monkeypatch.setattr(mod, "_TTY_LOOKUP_FAILED", False)

    class _Broken:
        returncode = 1
        stdout = ""
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Broken())

    assert mod._ancestor_tty_via_ps() is None
    assert mod._TTY_LOOKUP_FAILED is True


def test_a_failed_write_leaves_no_orphan_tmp(tmp_path, monkeypatch):
    """Exercises the except branch: nothing sweeps TTY_DIR, so a tmp left
    behind by a failed write would sit there forever."""
    mod = _load_hook_module()
    tty_dir = tmp_path / "tty"
    monkeypatch.setattr(mod, "TTY_DIR", str(tty_dir))
    monkeypatch.setattr(mod, "_controlling_tty", lambda: "/dev/ttys004")
    real_replace = os.replace

    def _fail_replace(src, dst):
        raise OSError("no space left on device")
    monkeypatch.setattr(os, "replace", _fail_replace)

    mod._record_session_tty("sess-a", "UserPromptSubmit")

    monkeypatch.setattr(os, "replace", real_replace)
    assert not (tty_dir / "sess-a").exists()
    assert list(tty_dir.glob("*.tmp")) == [], "left an orphan tmp file"


def test_a_usable_ps_snapshot_is_used_even_on_a_nonzero_exit(monkeypatch):
    """`ps` exits nonzero merely because it could not read some unrelated
    process, while still printing a usable table. Discarding that snapshot
    would permanently downgrade such a machine to the cwd guess."""
    mod = _load_hook_module()
    monkeypatch.setattr(mod, "_TTY_LOOKUP_FAILED", False)
    monkeypatch.setattr(os, "getpid", lambda: 42)

    class _Partial:
        returncode = 1
        stdout = "42 7 ??\n7 1 ttys004\n"      # our parent DOES have a tty
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Partial())

    assert mod._ancestor_tty_via_ps() == "/dev/ttys004"
    assert mod._TTY_LOOKUP_FAILED is False


def test_a_snapshot_missing_our_own_pid_is_a_failure(monkeypatch):
    """Our own process is always running, so its absence from the table means
    `ps` gave us a filtered/partial snapshot (sandboxed or shimmed), not that
    the walk legitimately reached the top without finding a terminal. Treating
    it as the latter would delete a valid registry entry."""
    mod = _load_hook_module()
    monkeypatch.setattr(mod, "_TTY_LOOKUP_FAILED", False)
    monkeypatch.setattr(os, "getpid", lambda: 42)

    class _Filtered:
        returncode = 0                   # exits clean...
        stdout = "999 1 ttys004\n"       # ...but we are not in it
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Filtered())

    assert mod._ancestor_tty_via_ps() is None
    assert mod._TTY_LOOKUP_FAILED is True


def test_unit_tests_do_not_touch_the_real_squid_pet_home(tmp_path):
    """Regression: _load_hook_module used to import the script without
    SQUID_PET_HOME set, so LOG_PATH bound to the developer's real
    ~/.squid-pet and any test reaching a _log() call appended to the live
    hook log the watcher reads. 13 such lines were found in it."""
    mod = _load_hook_module(tmp_path)
    real_home = os.path.expanduser("~/.squid-pet")
    for attr in ("LOG_PATH", "TTY_DIR", "FLAG_DIR", "FAILED_DIR"):
        value = getattr(mod, attr, None)
        if value:
            assert not str(value).startswith(real_home), (
                f"{attr} points at the real ~/.squid-pet: {value}"
            )


@pytest.mark.parametrize("payload", [
    ["not", "an", "object"],
    {"hook_event_name": [], "session_id": "abc"},
    {"hook_event_name": "Stop", "session_id": 123},
    {"hook_event_name": "Stop", "session_id": "../../escape"},
    {"hook_event_name": "Stop", "session_id": "a\u0000b"},
    {"hook_event_name": "Stop", "session_id": "\ud800"},
    {"hook_event_name": "Stop", "session_id": ".hidden"},
])
def test_malformed_payload_or_session_id_exits_zero_and_writes_no_flags(payload, home, tmp_path):
    """Only the hook's own log may be written; no flag file anywhere under
    tmp_path (the parent of home, so an escape out of home is caught too)."""
    r = _run(payload, home)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    written = [p for p in tmp_path.rglob("*")
               if p.is_file() and p != home / "claude_hook.log"]
    assert written == []


def test_shared_atomic_write_cleans_up_its_tmp_on_failure(tmp_path, monkeypatch):
    """Review round 2 (Claude #7): the awaiting flag and the tty registry share
    ONE tmp+rename helper, and it removes its dot-prefixed tmp when the write
    fails, then re-raises so the caller can log it."""
    mod = _load_hook_module(tmp_path)
    target = tmp_path / "d" / "sess-w"
    target.parent.mkdir()

    def _fail_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", _fail_replace)
    with pytest.raises(OSError):
        mod._atomic_write(str(target), "x")
    assert os.listdir(target.parent) == [], "left an orphan tmp file"


def test_shared_atomic_write_success_leaves_only_the_target(tmp_path):
    mod = _load_hook_module(tmp_path)
    target = tmp_path / "d" / "sess-w"
    target.parent.mkdir()
    mod._atomic_write(str(target), "hello")
    assert target.read_text() == "hello"
    assert os.listdir(target.parent) == ["sess-w"]


# ── round 4 #0: PermissionRequest is attribution-only ─────────────────────
# PermissionRequest fires whenever a decision is NEEDED -- including calls
# auto mode's classifier resolves itself with no prompt ever shown -- so it
# must never raise a wave (round 3 did). It records a short-lived pending
# hint in its OWN dir; the Notification permission_prompt for a prompt that
# is really on screen consumes EVERY live hint, each adding its own owner
# (round 6) -- and falls back to `parent` when none is live. Verified live: a
# real Notification is
# UNTAGGED even for a subagent's prompt; a subagent's PermissionRequest
# carries agent_id.
def _pending_path(home: Path, sid: str) -> Path:
    return home / "claude_permission_pending" / sid


def _pending(home: Path, sid: str) -> list[dict]:
    fp = _pending_path(home, sid)
    if not fp.exists():
        return []
    return [json.loads(line) for line in fp.read_text().splitlines()]


def _age_pending(home: Path, sid: str, seconds: float) -> None:
    """Backdate every pending hint (the TTL is read from each entry's ts)."""
    fp = _pending_path(home, sid)
    entries = _pending(home, sid)
    for e in entries:
        e["ts"] -= seconds
    fp.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _permission_request(sid: str, agent_id: str | None = None,
                        **extra) -> dict:
    payload = {"session_id": sid, "hook_event_name": "PermissionRequest",
               "tool_name": "Bash",
               "tool_input": {"command": "cat /secret/path"},
               "permission_suggestions": [{"type": "addRules"}], **extra}
    if agent_id is not None:
        payload["agent_id"] = agent_id
        payload["agent_type"] = "general-purpose"
    return payload


_UNTAGGED_NOTIFICATION = {"hook_event_name": "Notification",
                          "notification_type": "permission_prompt",
                          "message": "Claude needs your permission to use Bash"}


def _notify(home: Path, sid: str) -> subprocess.CompletedProcess:
    return _run({"session_id": sid, **_UNTAGGED_NOTIFICATION}, home)


def _real_prompt(home: Path, sid: str, agent_id: str | None = None) -> None:
    """A prompt actually shown to the user: PermissionRequest, then the
    untagged Notification that follows it."""
    assert _run(_permission_request(sid, agent_id), home).returncode == 0
    assert _notify(home, sid).returncode == 0


def _post(sid: str, agent_id: str | None = None,
          event: str = "PostToolUse") -> dict:
    payload = {"session_id": sid, "hook_event_name": event,
               "tool_name": "Bash"}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


# Required test 1: auto-approved call.
def test_auto_approved_permission_request_raises_no_wave(home):
    r = _run(_permission_request("sess-aa", "agent-aaa"), home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-aa").exists()
    visible = [n for n in os.listdir(home / "claude_awaiting_input")
               if not n.startswith(".")]
    assert visible == [], "a pending hint must never look like a wave"
    assert [e["agent_id"] for e in _pending(home, "sess-aa")] == ["agent-aaa"]
    assert "PermissionRequest sess-aa PENDING (helper) n=1" in (
        home / "claude_hook.log").read_text()


def test_main_thread_permission_request_is_a_parent_hint(home):
    _run(_permission_request("sess-aa2"), home)
    assert not _flag_path(home, "sess-aa2").exists()
    assert [e["agent_id"] for e in _pending(home, "sess-aa2")] == [None]


def test_auto_approved_call_drops_its_own_hint_when_the_tool_runs(home):
    """Its PostToolUse spends the hint, so a LATER real prompt from someone
    else is not paired with it (the parent's prompt stays the parent's)."""
    _run(_permission_request("sess-aa3", "agent-aaa"), home)
    _run(_post("sess-aa3", "agent-aaa"), home)
    assert _pending(home, "sess-aa3") == []
    _real_prompt(home, "sess-aa3")
    assert _owners(home, "sess-aa3") == {"parent"}


def test_a_tool_that_needed_no_permission_does_not_drop_another_callers_hint(home):
    _run(_permission_request("sess-aa4", "agent-aaa"), home)
    _run(_post("sess-aa4"), home)  # the parent's unrelated tool ran
    assert [e["agent_id"] for e in _pending(home, "sess-aa4")] == ["agent-aaa"]


# Required test 2: auto-denied call.
def test_auto_denied_call_raises_no_wave_and_drops_its_hint(home):
    _run(_permission_request("sess-ad", "agent-aaa"), home)
    r = _run(_post("sess-ad", "agent-aaa", "PermissionDenied"), home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-ad").exists()
    assert _pending(home, "sess-ad") == []
    log_text = (home / "claude_hook.log").read_text()
    assert "PermissionDenied sess-ad PERMDENIED_DROP n=1" in log_text
    assert "UNKNOWN_EVENT" not in log_text


def test_permission_denied_prefers_the_callers_own_hint(home):
    _run(_permission_request("sess-ad2"), home)
    _run(_permission_request("sess-ad2", "agent-aaa"), home)
    _run(_post("sess-ad2", "agent-aaa", "PermissionDenied"), home)
    assert [e["agent_id"] for e in _pending(home, "sess-ad2")] == [None]


def test_permission_denied_without_an_own_hint_drops_the_oldest(home):
    _run(_permission_request("sess-ad3", "agent-aaa"), home)
    _run(_permission_request("sess-ad3", "agent-bbb"), home)
    _run(_post("sess-ad3", None, "PermissionDenied"), home)
    assert [e["agent_id"] for e in _pending(home, "sess-ad3")] == ["agent-bbb"]


def test_permission_denied_still_clears_its_own_share_as_a_safety_net(home):
    """In case a future Claude Code fires both a Notification and a
    PermissionDenied for one prompt."""
    _real_prompt(home, "sess-ad4", "agent-aaa")
    _real_prompt(home, "sess-ad4")
    assert _owners(home, "sess-ad4") == {"parent", "agent:agent-aaa"}
    _run(_post("sess-ad4", "agent-aaa", "PermissionDenied"), home)
    assert _owners(home, "sess-ad4") == {"parent"}
    _run(_post("sess-ad4", None, "PermissionDenied"), home)
    assert not _flag_path(home, "sess-ad4").exists()


# Required test 3: a real helper prompt.
def test_real_helper_prompt_is_owned_by_the_helper(home):
    _run(_permission_request("sess-rh", "agent-aaa"), home)
    r = _notify(home, "sess-rh")
    assert r.returncode == 0, r.stderr
    lines = _flag_path(home, "sess-rh").read_text().splitlines()
    assert lines[0] == "permission_prompt"
    assert _owners(home, "sess-rh") == {"agent:agent-aaa"}
    assert _pending(home, "sess-rh") == [], "the hint is consumed"
    assert "Notification sess-rh WRITE permission_prompt (helper via pending)" in (
        home / "claude_hook.log").read_text()


def test_real_helper_prompt_then_own_post_tool_use_clears(home):
    """The original bug, end to end: the helper's own approval clears its
    own wave the moment its tool runs."""
    _real_prompt(home, "sess-rh2", "agent-aaa")
    r = _run(_post("sess-rh2", "agent-aaa"), home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-rh2").exists()


def test_real_parent_prompt_is_owned_by_the_parent(home):
    _real_prompt(home, "sess-rh3")
    assert _owners(home, "sess-rh3") == {"parent"}
    assert "(parent via pending)" in (home / "claude_hook.log").read_text()


# Required test 4: no pending hint.
def test_notification_without_a_hint_falls_back_to_parent(home):
    _run(_permission_request("sess-other", "agent-aaa"), home)  # other session
    r = _notify(home, "sess-nh")
    assert r.returncode == 0, r.stderr
    assert _owners(home, "sess-nh") == {"parent"}
    assert "Notification sess-nh WRITE permission_prompt (parent via fallback)" in (
        home / "claude_hook.log").read_text()


# Required test 5: an expired hint is never used.
def test_pending_ttl_is_tight(home):
    """Round 5 (Codex P1, defence in depth): a real prompt's Notification
    follows its PermissionRequest almost at once, so the TTL is short -- not
    the round-4 30s that let a still-running auto-approved call's hint linger.
    Round 6: raised 5s -> 10s as a cheap hedge against an unverified reviewer
    citation (see the comment above _PENDING_TTL_SEC) -- still short relative
    to round 4's 30s, and no longer safety-critical either way since round 6
    makes an ambiguous/expired hint safe (co-owner or `parent` fallback,
    never a wrong guess)."""
    mod = _load_hook_module(home)
    assert mod._PENDING_TTL_SEC == 10.0


def test_expired_hint_falls_back_to_parent(home):
    _run(_permission_request("sess-ex", "agent-aaa"), home)
    _age_pending(home, "sess-ex", 11)
    _notify(home, "sess-ex")
    assert _owners(home, "sess-ex") == {"parent"}
    assert _pending(home, "sess-ex") == [], "the expired hint is pruned"


def test_hint_just_inside_the_ttl_is_used(home):
    _run(_permission_request("sess-ex2", "agent-aaa"), home)
    _age_pending(home, "sess-ex2", 8)
    _notify(home, "sess-ex2")
    assert _owners(home, "sess-ex2") == {"agent:agent-aaa"}


# Required test 6 (round 6, replaces round 5's "two or more -> `parent`,
# consume none"): the Notification never picks ONE of several live hints --
# every live hint becomes its own owner line, and each clears only via its
# own caller's resolution.
@pytest.mark.parametrize("first,second,owners,who", [
    (None, "agent-aaa", {"parent", "agent:agent-aaa"}, "parent+helper"),
    ("agent-aaa", None, {"parent", "agent:agent-aaa"}, "parent+helper"),
    ("agent-aaa", "agent-bbb", {"agent:agent-aaa", "agent:agent-bbb"}, "helper"),
])
def test_every_live_hint_becomes_an_owner_and_all_are_consumed(
        home, first, second, owners, who):
    """A Notification carries no agent_id, so with two requests live at once
    the hook cannot tell whose prompt is on screen. It guesses neither and
    drops neither: each live hint adds its OWN owner (`agent:<id>`, or
    `parent` for an untagged hint), all in one write, and all are consumed."""
    sid = "sess-amb"
    _run(_permission_request(sid, first), home)
    _run(_permission_request(sid, second), home)
    r = _notify(home, sid)
    assert r.returncode == 0, r.stderr
    assert _owners(home, sid) == owners
    assert _pending(home, sid) == [], "every live hint is consumed"
    lines = _flag_path(home, sid).read_text().splitlines()
    assert lines[0] == "permission_prompt"
    assert len({ln.split(" ")[1] for ln in lines[1:]}) == 1, (
        "all owners are added in ONE write (one add time)")
    assert (f"Notification {sid} WRITE permission_prompt ({who} via pending n=2)"
            in (home / "claude_hook.log").read_text())


def test_duplicate_hints_from_one_caller_add_one_owner(home):
    sid = "sess-dup"
    _run(_permission_request(sid, "agent-aaa"), home)
    _run(_permission_request(sid, "agent-aaa"), home)
    _notify(home, sid)
    assert _owners(home, sid) == {"agent:agent-aaa"}
    assert _pending(home, sid) == []


def test_only_live_hints_become_owners(home):
    """An expired hint is pruned, never an owner: nothing is synthesised."""
    sid = "sess-mix"
    _run(_permission_request(sid), home)
    _age_pending(home, sid, 11)          # the parent's hint has expired
    _run(_permission_request(sid, "agent-aaa"), home)
    _notify(home, sid)
    assert _owners(home, sid) == {"agent:agent-aaa"}
    assert _pending(home, sid) == []


def _parent_event(sid: str, event: str) -> dict:
    payload = {"session_id": sid, "hook_event_name": event}
    if event in ("PostToolUse", "PostToolUseFailure"):
        payload["tool_name"] = "Bash"
    return payload


def _helper_event(sid: str, agent_id: str, event: str) -> dict:
    payload = {"session_id": sid, "hook_event_name": event,
               "agent_id": agent_id, "agent_type": "general-purpose"}
    if event in ("PostToolUse", "PostToolUseFailure"):
        payload["tool_name"] = "Bash"
    return payload


@pytest.mark.parametrize("parent_event",
                         ["PostToolUse", "Stop", "UserPromptSubmit"])
@pytest.mark.parametrize("helper_event",
                         ["PostToolUse", "PostToolUseFailure", "SubagentStop"])
def test_parent_resolution_cannot_clear_a_co_owning_helpers_wave(
        home, parent_event, helper_event):
    """Codex round 5 P1, exact scenario. {parent, helperX} hints are both
    live; the real prompt is helperX's. Round 5 attributed it to bare
    `parent`, so the parent's own next UNRELATED clearing event wiped the wave
    while helperX's prompt was still unanswered. Now both are co-owners:
    the parent's event removes only its own line, and the wave survives
    until helperX's OWN resolution."""
    sid = "sess-co"
    _run(_permission_request(sid), home)                  # parent's hint
    _real_prompt(home, sid, "agent-xxx")                  # helperX's prompt
    assert _owners(home, sid) == {"parent", "agent:agent-xxx"}

    r = _run(_parent_event(sid, parent_event), home)
    assert r.returncode == 0, r.stderr
    assert _owners(home, sid) == {"agent:agent-xxx"}, (
        "the parent's own resolution must leave helperX's wave up")

    r = _run(_helper_event(sid, "agent-xxx", helper_event), home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, sid).exists(), (
        "helperX's own resolution is what finally clears it")


@pytest.mark.parametrize("helper_event",
                         ["PostToolUse", "PostToolUseFailure", "SubagentStop"])
def test_two_co_owning_helpers_each_clear_only_via_their_own_resolution(
        home, helper_event):
    sid = "sess-co2"
    _run(_permission_request(sid, "agent-aaa"), home)
    _real_prompt(home, sid, "agent-bbb")
    both = {"agent:agent-aaa", "agent:agent-bbb"}
    assert _owners(home, sid) == both
    # Third parties' actions touch neither share.
    for payload in (_parent_event(sid, "PostToolUse"),
                    _parent_event(sid, "Stop"),
                    _helper_event(sid, "agent-ccc", helper_event)):
        assert _run(payload, home).returncode == 0
        assert _owners(home, sid) == both
    _run(_helper_event(sid, "agent-aaa", helper_event), home)
    assert _owners(home, sid) == {"agent:agent-bbb"}
    _run(_helper_event(sid, "agent-bbb", helper_event), home)
    assert not _flag_path(home, sid).exists()


def test_stale_auto_approved_hint_cannot_hijack_another_partys_prompt(home):
    """Codex round 4 P1, exact scenario. A helper's call is auto-approved:
    its PermissionRequest hint is recorded but its tool is still running (no
    PostToolUse yet). Meanwhile the PARENT's real prompt comes up (its own
    PermissionRequest, then the untagged Notification) inside the TTL. Both
    become co-owners (round 6); when the helper's delayed PostToolUse finally
    fires it removes only its own line -- the parent's wave survives."""
    sid = "sess-hj"
    _run(_permission_request(sid, "agent-aaa"), home)   # auto-approved, running
    _real_prompt(home, sid)                              # parent's real prompt
    assert _owners(home, sid) == {"parent", "agent:agent-aaa"}
    assert _pending(home, sid) == []
    r = _run(_post(sid, "agent-aaa"), home)              # helper's delayed post
    assert r.returncode == 0, r.stderr
    assert _owners(home, sid) == {"parent"}, "the parent's wave survives"
    assert (f"PostToolUse {sid} HELPER_CLEAR_OWN (agent_id) KEPT"
            in (home / "claude_hook.log").read_text())
    _run(_post(sid), home)                               # parent's own post
    assert not _flag_path(home, sid).exists()


def test_stale_hint_between_two_helpers_cannot_hijack_either(home):
    """Same as above with two helpers: agent-aaa auto-approved and still
    running, agent-bbb's prompt really shown."""
    sid = "sess-hj2"
    _run(_permission_request(sid, "agent-aaa"), home)
    _real_prompt(home, sid, "agent-bbb")
    _run(_post(sid, "agent-aaa"), home)
    assert _owners(home, sid) == {"agent:agent-bbb"}, (
        "agent-bbb's wave survives agent-aaa's unrelated PostToolUse")
    _run(_post(sid, "agent-bbb"), home)
    assert not _flag_path(home, sid).exists()


def test_tagged_notification_uses_its_own_tag(home):
    """Only synthetic Notifications carry agent_id; if one does, it is
    authoritative and consumes that agent's own hint, not the oldest."""
    _run(_permission_request("sess-tg"), home)
    _run(_permission_request("sess-tg", "agent-aaa"), home)
    _run({"session_id": "sess-tg", **_UNTAGGED_NOTIFICATION,
          "agent_id": "agent-aaa"}, home)
    assert _owners(home, "sess-tg") == {"agent:agent-aaa"}
    assert [e["agent_id"] for e in _pending(home, "sess-tg")] == [None]


# Required test 7: PostToolUseFailure clears like PostToolUse.
def test_parent_post_tool_use_failure_clears_the_parents_share(home):
    _real_prompt(home, "sess-tf")
    _real_prompt(home, "sess-tf", "agent-aaa")
    r = _run(_post("sess-tf", None, "PostToolUseFailure"), home)
    assert r.returncode == 0, r.stderr
    assert _owners(home, "sess-tf") == {"agent:agent-aaa"}
    log_text = (home / "claude_hook.log").read_text()
    assert "PostToolUseFailure sess-tf KEPT (helper owner pending)" in log_text
    assert "UNKNOWN_EVENT" not in log_text


def test_helper_post_tool_use_failure_clears_its_own_share(home):
    _real_prompt(home, "sess-tf2")
    _real_prompt(home, "sess-tf2", "agent-aaa")
    r = _run(_post("sess-tf2", "agent-aaa", "PostToolUseFailure"), home)
    assert r.returncode == 0, r.stderr
    assert _owners(home, "sess-tf2") == {"parent"}
    assert "PostToolUseFailure sess-tf2 HELPER_CLEAR_OWN (agent_id) KEPT" in (
        home / "claude_hook.log").read_text()


def test_helper_post_tool_use_failure_touches_no_other_parent_state(home):
    _seed_parent_flags(home, "sess-tf3")
    before = _snapshot_parent_flags(home, "sess-tf3")
    _run(_post("sess-tf3", "agent-aaa", "PostToolUseFailure"), home)
    assert _snapshot_parent_flags(home, "sess-tf3") == before


# Pending-store bookkeeping.
def test_pending_hints_are_capped_per_session(home):
    for i in range(10):
        _run(_permission_request("sess-cap", f"agent-{i}"), home)
    assert [e["agent_id"] for e in _pending(home, "sess-cap")] == [
        f"agent-{i}" for i in range(2, 10)], "the oldest are dropped"


def test_expired_hints_are_pruned_on_the_next_request(home):
    _run(_permission_request("sess-pr", "agent-old"), home)
    _age_pending(home, "sess-pr", 60)
    _run(_permission_request("sess-pr", "agent-new"), home)
    assert [e["agent_id"] for e in _pending(home, "sess-pr")] == ["agent-new"]


def test_tool_use_id_is_recorded_only_when_a_plain_id(home):
    _run(_permission_request("sess-tu", tool_use_id="toolu_01AbC"), home)
    _run(_permission_request("sess-tu", tool_use_id="bad\nid"), home)
    assert [e["tool_use_id"] for e in _pending(home, "sess-tu")] == [
        "toolu_01AbC", None]


def test_stop_drops_only_the_parents_hints(home):
    _run(_permission_request("sess-sd"), home)
    _run(_permission_request("sess-sd", "agent-aaa"), home)
    _run({"session_id": "sess-sd", "hook_event_name": "Stop"}, home)
    assert [e["agent_id"] for e in _pending(home, "sess-sd")] == ["agent-aaa"]


def test_subagent_stop_drops_only_that_helpers_hints(home):
    _run(_permission_request("sess-ss"), home)
    _run(_permission_request("sess-ss", "agent-aaa"), home)
    _run(_permission_request("sess-ss", "agent-aaa"), home)
    _run(_post("sess-ss", "agent-aaa", "SubagentStop"), home)
    assert [e["agent_id"] for e in _pending(home, "sess-ss")] == [None]


def test_session_end_removes_the_pending_store(home):
    _run(_permission_request("sess-se"), home)
    assert _pending_path(home, "sess-se").exists()
    r = _run({"session_id": "sess-se", "hook_event_name": "SessionEnd"}, home)
    assert r.returncode == 0, r.stderr
    assert not _pending_path(home, "sess-se").exists()


def test_session_end_pending_removal_waits_for_an_in_flight_publish(
        tmp_path, monkeypatch):
    """Codex round 4 P2: SessionEnd used to unlink the pending store with no
    lock, so a PermissionRequest mid-publish (holding the lock, tmp written,
    rename pending) could land its file right after -- an orphan. The removal
    must take the same lock and only then look. Made deterministic like the
    flag-removal race test above: once SessionEnd's flag removal is done, the
    test takes the lock (standing in for the in-flight publish), waits until
    SessionEnd reaches flock again, publishes, then releases."""
    import fcntl
    import io
    import threading

    mod = _load_hook_module(tmp_path)
    os.makedirs(mod.FLAG_DIR)
    os.makedirs(mod.PENDING_DIR)
    pending = Path(mod.PENDING_DIR) / "sess-sr"
    holder = open(os.path.join(mod.FLAG_DIR, ".lock"), "a")
    real_flock = fcntl.flock
    publisher_in = threading.Event()
    reached = threading.Event()

    def spy(fd, op):
        if (publisher_in.is_set()
                and threading.current_thread() is not threading.main_thread()):
            reached.set()
        return real_flock(fd, op)
    monkeypatch.setattr(mod.fcntl, "flock", spy)

    real_remove_entirely = mod._remove_flag_entirely

    def remove_then_let_the_publisher_in(sid):
        result = real_remove_entirely(sid)
        real_flock(holder, fcntl.LOCK_EX)  # the publish now holds the lock
        publisher_in.set()
        return result
    monkeypatch.setattr(mod, "_remove_flag_entirely",
                        remove_then_let_the_publisher_in)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "sess-sr", "hook_event_name": "SessionEnd"})))

    rc: list = []
    t = threading.Thread(target=lambda: rc.append(mod.main()))
    t.start()
    try:
        assert publisher_in.wait(5)
        assert reached.wait(2), (
            "SessionEnd removed the pending store without taking the lock "
            f"(rc={rc!r})")
        assert t.is_alive()
        pending.write_text('{"agent_id": null, "tool_use_id": null, "ts": 1}\n')
    finally:
        real_flock(holder, fcntl.LOCK_UN)
        holder.close()
        t.join(5)
    assert rc == [0]
    assert not pending.exists(), "the in-flight publish was left orphaned"


def test_session_end_pending_removal_failure_still_exits_zero(home):
    """The locked removal needs the flag dir for its lock; if the pending
    removal fails, SessionEnd still exits 0 and still closes the turn."""
    _run(_permission_request("sess-sf", "agent-aaa"), home)
    _run({"session_id": "sess-sf", "hook_event_name": "UserPromptSubmit"}, home)
    assert _pending_path(home, "sess-sf").exists()
    (home / "claude_permission_pending").chmod(0o500)
    try:
        r = _run({"session_id": "sess-sf", "hook_event_name": "SessionEnd"}, home)
    finally:
        (home / "claude_permission_pending").chmod(0o700)
    assert r.returncode == 0, r.stderr
    assert not _turn_active_path(home, "sess-sf").exists()
    assert "SessionEnd sess-sf PENDING_REMOVE_FAILED" in (
        home / "claude_hook.log").read_text()


def test_clearing_event_with_no_pending_store_creates_nothing(home):
    _run(_post("sess-np"), home)
    assert not (home / "claude_permission_pending").exists()


def test_corrupt_pending_store_falls_back_to_parent(home):
    _pending_path(home, "sess-cp").parent.mkdir(parents=True)
    _pending_path(home, "sess-cp").write_text('not json\n[1]\n{"ts": "x"}\n')
    r = _notify(home, "sess-cp")
    assert r.returncode == 0, r.stderr
    assert _owners(home, "sess-cp") == {"parent"}


def test_permission_request_is_content_blind(home):
    _run(_permission_request("sess-cb", "agent-aaa"), home)
    _notify(home, "sess-cb")
    for fp in (_pending_path(home, "sess-cb"), _flag_path(home, "sess-cb"),
               home / "claude_hook.log"):
        if fp.exists():
            text = fp.read_text()
            assert "secret" not in text and "addRules" not in text, fp


def test_helper_permission_request_mutates_no_parent_state(home):
    _seed_parent_flags(home, "sess-pm")
    before = _snapshot_parent_flags(home, "sess-pm")
    _run(_permission_request("sess-pm", "agent-aaa"), home)
    assert _snapshot_parent_flags(home, "sess-pm") == before


def test_bad_agent_id_permission_request_is_a_parent_hint(home):
    r = _run(_permission_request("sess-ba", ""), home)
    assert r.returncode == 0, r.stderr
    assert [e["agent_id"] for e in _pending(home, "sess-ba")] == [None]
    assert "PermissionRequest sess-ba BAD_AGENT_ID" in (
        home / "claude_hook.log").read_text()
    _notify(home, "sess-ba")
    assert _owners(home, "sess-ba") == {"parent"}


def test_other_notification_types_ignore_the_pending_store(home):
    _run(_permission_request("sess-it"), home)
    r = _run({"session_id": "sess-it", "hook_event_name": "Notification",
              "notification_type": "idle_prompt"}, home)
    assert r.returncode == 0, r.stderr
    assert not _flag_path(home, "sess-it").exists()
    assert len(_pending(home, "sess-it")) == 1


def test_parent_permission_denied_leaves_a_helper_wave(home):
    _real_prompt(home, "sess-pd2", "agent-aaa")
    _run(_post("sess-pd2", None, "PermissionDenied"), home)
    assert _owners(home, "sess-pd2") == {"agent:agent-aaa"}


def test_parent_permission_denied_does_not_touch_turn_or_finished(home):
    _seed_parent_flags(home, "sess-pd3")
    _run(_post("sess-pd3", None, "PermissionDenied"), home)
    after = _snapshot_parent_flags(home, "sess-pd3")
    assert after["claude_awaiting_input"] is None
    assert after["claude_turn_active"] == "turn"
    assert after["claude_failed"] == "overloaded_error"
    assert after["claude_finished"] is None


# ── round 3 #1: present-but-falsy agent_id is a helper, never main thread ──
@pytest.mark.parametrize("falsy", ["", [], {}, 0])
def test_falsy_agent_id_cannot_clear_the_parents_wave(home, falsy):
    """Before: `bool(agent_id)` read a falsy id as a MAIN-THREAD event, so a
    helper's PostToolUse carrying agent_id "" cleared the parent's wave."""
    _run({"session_id": "sess-f1", **_UNTAGGED_NOTIFICATION}, home)
    r = _run({"session_id": "sess-f1", "hook_event_name": "Stop",
              "agent_id": falsy}, home)
    assert r.returncode == 0, r.stderr
    assert _owners(home, "sess-f1") == {"parent"}
    assert not _finished_path(home, "sess-f1").exists()
    log_text = (home / "claude_hook.log").read_text()
    assert "Stop sess-f1 BAD_AGENT_ID" in log_text


def test_null_agent_id_is_a_main_thread_event(home):
    _run({"session_id": "sess-f2", **_UNTAGGED_NOTIFICATION}, home)
    _run({"session_id": "sess-f2", "hook_event_name": "Stop",
          "agent_id": None}, home)
    assert not _flag_path(home, "sess-f2").exists()
    assert _finished_path(home, "sess-f2").exists()


# ── round 3 #2: owner add times, mtime, and ageing out ────────────────────
def _owner_times(home: Path, sid: str) -> dict[str, float]:
    return {line.split()[0]: float(line.split()[1])
            for line in _flag_path(home, sid).read_text().splitlines()[1:]}


def _write_flag(home: Path, sid: str, lines: list[str], mtime: float) -> Path:
    d = home / "claude_awaiting_input"
    d.mkdir(parents=True, exist_ok=True)
    fp = d / sid
    fp.write_text("\n".join(["permission_prompt", *lines]))
    os.utime(fp, (mtime, mtime))
    return fp


def test_owner_lines_carry_their_add_time_and_it_is_the_mtime(home):
    import time as _time
    before = _time.time()
    _real_prompt(home, "sess-m1", "agent-aaa")
    after = _time.time()
    times = _owner_times(home, "sess-m1")
    assert before - 0.01 <= times["agent:agent-aaa"] <= after + 0.01
    mtime = _flag_path(home, "sess-m1").stat().st_mtime
    assert abs(mtime - times["agent:agent-aaa"]) < 0.001


def test_partial_clear_of_an_older_owner_keeps_the_mtime_exactly(home):
    import time as _time
    now = _time.time()
    fp = _write_flag(home, "sess-m2", [f"agent:agent-aaa {now - 600:.6f}",
                                       f"agent:agent-bbb {now - 500:.6f}"],
                     now - 500)
    before_ns = fp.stat().st_mtime_ns
    _run({"session_id": "sess-m2", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-aaa"}, home)
    assert _owners(home, "sess-m2") == {"agent:agent-bbb"}
    assert fp.stat().st_mtime_ns == before_ns


def test_partial_clear_of_the_newest_owner_drops_to_the_newest_remaining(home):
    """mtime = newest remaining owner's add time: it never ADVANCES on a
    clear (the watcher re-arms on an advance), and the surviving older wave
    ages from its own raise."""
    import time as _time
    now = _time.time()
    fp = _write_flag(home, "sess-m3", [f"agent:agent-aaa {now - 600:.6f}",
                                       f"agent:agent-bbb {now - 500:.6f}"],
                     now - 500)
    before = fp.stat().st_mtime
    _run({"session_id": "sess-m3", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-bbb"}, home)
    assert _owners(home, "sess-m3") == {"agent:agent-aaa"}
    assert abs(fp.stat().st_mtime - (now - 600)) < 0.01
    assert fp.stat().st_mtime <= before


def test_a_new_add_advances_the_mtime(home):
    import time as _time
    now = _time.time()
    fp = _write_flag(home, "sess-m4", [f"agent:agent-aaa {now - 600:.6f}"],
                     now - 600)
    _real_prompt(home, "sess-m4")
    assert fp.stat().st_mtime > now - 1
    assert _owners(home, "sess-m4") == {"parent", "agent:agent-aaa"}


def test_a_dead_owner_ages_out_even_while_new_prompts_keep_arriving(home):
    """Review #6: before, every add refreshed the file mtime, so a helper that
    died with a pending share was never swept while its siblings kept
    raising prompts. Owners older than 7200s are pruned on every add."""
    import time as _time
    now = _time.time()
    _write_flag(home, "sess-m5", [f"agent:agent-dead {now - 7300:.6f}",
                                  f"agent:agent-live {now - 100:.6f}"],
                now - 100)
    _real_prompt(home, "sess-m5", "agent-new")
    assert _owners(home, "sess-m5") == {"agent:agent-live", "agent:agent-new"}


def test_a_dead_owner_is_pruned_on_remove_too(home):
    import time as _time
    now = _time.time()
    _write_flag(home, "sess-m6", [f"agent:agent-dead {now - 7300:.6f}",
                                  f"agent:agent-live {now - 100:.6f}"],
                now - 100)
    _run({"session_id": "sess-m6", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "agent_id": "agent-live"}, home)
    assert not _flag_path(home, "sess-m6").exists(), (
        "the live owner was the last one that could ever report back")


def test_legacy_owner_line_without_a_time_uses_the_file_mtime(home):
    import time as _time
    now = _time.time()
    _write_flag(home, "sess-m7", ["agent:agent-old"], now - 7300)
    _real_prompt(home, "sess-m7")
    assert _owners(home, "sess-m7") == {"parent"}
    fresh = _write_flag(home, "sess-m8", ["agent:agent-old"], now - 50)
    _real_prompt(home, "sess-m8")
    assert _owners(home, "sess-m8") == {"parent", "agent:agent-old"}
    assert abs(_owner_times(home, "sess-m8")["agent:agent-old"] - (now - 50)) < 0.01
    assert fresh.exists()


def test_atomic_write_stamps_the_tmp_before_the_rename(tmp_path, monkeypatch):
    """Review #5: the mtime is set on the TEMP file, so the published flag
    never exists -- not even for an instant -- with a fresh mtime."""
    mod = _load_hook_module(tmp_path)
    target = tmp_path / "d" / "sess-w"
    target.parent.mkdir()
    stamped: list[str] = []
    real_utime = os.utime

    def spy(path, *a, **k):
        stamped.append(os.path.basename(str(path)))
        return real_utime(path, *a, **k)
    monkeypatch.setattr(os, "utime", spy)
    mod._atomic_write(str(target), "x", mtime_ns=1_000_000_000_000_000_000)
    assert target.stat().st_mtime_ns == 1_000_000_000_000_000_000
    assert stamped and all(n.startswith(".") and n.endswith(".tmp")
                           for n in stamped), stamped
    assert os.listdir(target.parent) == ["sess-w"]


# ── round 3 #4: a read error aborts -- never fabricate a `parent` owner ────
_needs_perms = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permissions")


@_needs_perms
@pytest.mark.parametrize("payload", [
    {"session_id": "sess-rf", **_UNTAGGED_NOTIFICATION},
    {"session_id": "sess-rf", "hook_event_name": "PostToolUse",
     "tool_name": "Bash", "agent_id": "agent-aaa"},
    {"session_id": "sess-rf", "hook_event_name": "PostToolUse",
     "tool_name": "Bash"},
])
def test_unreadable_flag_is_left_exactly_as_it_was(home, payload):
    import time as _time
    now = _time.time()
    fp = _write_flag(home, "sess-rf", [f"agent:agent-aaa {now:.6f}"], now)
    content = fp.read_text()
    fp.chmod(0)
    try:
        r = _run(payload, home)
    finally:
        fp.chmod(0o644)
    assert r.returncode == 0, r.stderr
    assert fp.read_text() == content, "a read failure must not rewrite the flag"
    log_text = (home / "claude_hook.log").read_text()
    assert "sess-rf READ_FAILED EACCES" in log_text


@_needs_perms
def test_unreadable_flag_on_stop_still_closes_the_turn(home):
    import time as _time
    now = _time.time()
    fp = _write_flag(home, "sess-rf2", [f"parent {now:.6f}"], now)
    _turn_active_path(home, "sess-rf2").parent.mkdir(parents=True)
    _turn_active_path(home, "sess-rf2").write_text("turn")
    fp.chmod(0)
    try:
        r = _run({"session_id": "sess-rf2", "hook_event_name": "Stop"}, home)
    finally:
        fp.chmod(0o644)
    assert r.returncode == 0, r.stderr
    assert fp.exists()
    assert not _turn_active_path(home, "sess-rf2").exists()
    assert _finished_path(home, "sess-rf2").exists()
