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
    assert fp.read_text() == "permission_prompt"


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


def _load_hook_module():
    spec = _ilu.spec_from_file_location("claude_pet_hook", SCRIPT)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
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
    mod._record_session_tty("sess-cursor")
    assert (tmp_path / "tty" / "sess-cursor").read_text() == "/dev/ttys008"


def test_records_nothing_when_there_is_no_tty(tmp_path, monkeypatch):
    """A detached/headless spawn has no controlling terminal -- leave no
    entry (the reader falls back to cwd) rather than crashing the hook."""
    mod = _load_hook_module()
    monkeypatch.setattr(mod, "TTY_DIR", str(tmp_path / "tty"))
    monkeypatch.setattr(mod, "_controlling_tty", lambda: None)
    mod._record_session_tty("sess-x")
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

