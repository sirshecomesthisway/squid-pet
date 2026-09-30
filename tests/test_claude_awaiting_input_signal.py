"""Pink-2026-08-26: Claude-Code-native sibling of the legacy agent's
direct-signal tests (since removed).

Background: that agent's approval_needed alert was driven by a private
sitecustomize.py patch writing a PID-keyed flag file.
Claude Code has no equivalent private hook, but DOES have an official
Notification hook. scripts/claude_pet_hook.py is wired into
~/.claude/settings.json (Notification/UserPromptSubmit/SessionEnd) and
writes/removes ~/.squid-pet/claude_awaiting_input/<session_id> the same
way -- except keyed by session_id (no PID in the hook payload) instead
of PID, and with NO engagement gate (see filter_eligible_claude_sessions'
docstring for why one isn't needed here, unlike the legacy path's).

These tests exercise watcher.py's side only (claude_sessions_awaiting_input,
filter_eligible_claude_sessions, the compute() integration, and the
extended snooze_all_awaiting_now/count_currently_waving_pids). The hook
script itself (scripts/claude_pet_hook.py) is tested separately in
tests/test_claude_pet_hook_script.py by invoking it as a real subprocess.
"""
from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

from squid_pet import watcher


@pytest.fixture
def tmp_claude_dir(tmp_path, monkeypatch):
    """Redirect the Claude awaiting-input dir to a tmp path for the test."""
    d = tmp_path / "claude_awaiting_input"
    d.mkdir()
    monkeypatch.setattr(watcher, "CLAUDE_AWAITING_INPUT_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _clear_claude_session_state():
    """_CLAUDE_SESSION_FLAG_FIRST_SEEN is module-level state -- reset
    around every test so tests can't leak into each other."""
    watcher._CLAUDE_SESSION_FLAG_FIRST_SEEN.clear()
    watcher._CLAUDE_SESSION_FLAG_MTIME.clear()
    yield
    watcher._CLAUDE_SESSION_FLAG_FIRST_SEEN.clear()
    watcher._CLAUDE_SESSION_FLAG_MTIME.clear()


@pytest.fixture(autouse=True)
def tmp_projects_dir(tmp_path, monkeypatch):
    """Self-heal reads subagent transcript mtimes under CLAUDE_PROJECTS_DIR
    (2026-09-30 round 2): never let a test touch the real ~/.claude/projects.
    Not created -- a missing dir means "no helpers", the common case."""
    d = tmp_path / "projects"
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(d))
    return d


# ── claude_sessions_awaiting_input() ──────────────────────────────────

def test_no_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "CLAUDE_AWAITING_INPUT_DIR",
                        str(tmp_path / "nope"))
    assert watcher.claude_sessions_awaiting_input() == []


def test_empty_dir_returns_empty(tmp_claude_dir):
    assert watcher.claude_sessions_awaiting_input() == []


def test_fresh_flag_is_reported(tmp_claude_dir):
    (tmp_claude_dir / "sess-abc").write_text("permission_prompt")
    assert watcher.claude_sessions_awaiting_input() == ["sess-abc"]


def test_multiple_sessions_all_reported_sorted(tmp_claude_dir):
    (tmp_claude_dir / "sess-b").write_text("permission_prompt")
    (tmp_claude_dir / "sess-a").write_text("permission_prompt")
    assert watcher.claude_sessions_awaiting_input() == ["sess-a", "sess-b"]


def test_dotfiles_are_ignored(tmp_claude_dir):
    (tmp_claude_dir / "sess-real").write_text("permission_prompt")
    (tmp_claude_dir / ".DS_Store").write_text("junk")
    assert watcher.claude_sessions_awaiting_input() == ["sess-real"]


def test_stale_flag_is_evicted(tmp_claude_dir, monkeypatch):
    """A flag older than CLAUDE_AWAITING_INPUT_STALE_SEC is treated as a
    crashed session (never fired UserPromptSubmit/SessionEnd) and both
    removed from disk and excluded from the result."""
    import os
    f = tmp_claude_dir / "sess-dead"
    f.write_text("permission_prompt")
    old = time.time() - watcher.CLAUDE_AWAITING_INPUT_STALE_SEC - 60
    os.utime(f, (old, old))

    assert watcher.claude_sessions_awaiting_input() == []
    assert not f.exists(), "stale flag should be deleted from disk"


def test_fresh_flag_just_under_stale_threshold_survives(tmp_claude_dir):
    import os
    f = tmp_claude_dir / "sess-almost-stale"
    f.write_text("permission_prompt")
    fresh = time.time() - (watcher.CLAUDE_AWAITING_INPUT_STALE_SEC - 60)
    os.utime(f, (fresh, fresh))
    assert watcher.claude_sessions_awaiting_input() == ["sess-almost-stale"]


# ── filter_eligible_claude_sessions() ─────────────────────────────────

def test_fresh_session_is_eligible():
    assert watcher.filter_eligible_claude_sessions(["sess-1"]) == ["sess-1"]


def test_no_engagement_gate_unlike_the_legacy_path():
    """Unlike the legacy filter_eligible_awaiting_pids, a session with no prior
    activity tracking still fires -- Claude's Notification hook only
    ever fires mid/post-turn, so there's no 'fresh, never engaged' class
    of false positive to gate against."""
    assert watcher.filter_eligible_claude_sessions(["brand-new-session"]) \
        == ["brand-new-session"]


def test_session_snoozes_after_window():
    now = 1_000_000.0
    with patch("time.time", return_value=now):
        assert watcher.filter_eligible_claude_sessions(["sess-1"]) == ["sess-1"]
    later = now + watcher._CLAUDE_SESSION_SNOOZE_SEC + 1
    with patch("time.time", return_value=later):
        assert watcher.filter_eligible_claude_sessions(["sess-1"]) == []


def test_session_rearms_after_flag_disappears_and_reappears():
    now = 1_000_000.0
    with patch("time.time", return_value=now):
        watcher.filter_eligible_claude_sessions(["sess-1"])
    stale = now + watcher._CLAUDE_SESSION_SNOOZE_SEC + 1
    with patch("time.time", return_value=stale):
        assert watcher.filter_eligible_claude_sessions(["sess-1"]) == []
        # Flag disappears (user replied) -- eviction happens on next call
        # with sess-1 absent from the live set.
        assert watcher.filter_eligible_claude_sessions([]) == []
        assert "sess-1" not in watcher._CLAUDE_SESSION_FLAG_FIRST_SEEN
    # Fresh prompt/permission wait -> new birth time -> eligible again.
    reappear = stale + 1
    with patch("time.time", return_value=reappear):
        assert watcher.filter_eligible_claude_sessions(["sess-1"]) == ["sess-1"]


def test_independent_sessions_tracked_separately():
    now = 1_000_000.0
    with patch("time.time", return_value=now):
        watcher.filter_eligible_claude_sessions(["sess-old"])
    later = now + watcher._CLAUDE_SESSION_SNOOZE_SEC + 1
    with patch("time.time", return_value=later):
        result = watcher.filter_eligible_claude_sessions(["sess-old", "sess-new"])
    assert result == ["sess-new"], \
        "sess-old should be snoozed, sess-new (first seen this tick) should not"


# ── Integration: compute() fires approval_needed from a Claude session ─

def _patched_config(**overrides):
    defaults = {
        "approval_alert_enabled": True,
        "approval_alert_sound": "Glass",
        "approval_alert_text": "your turn",
    }
    defaults.update(overrides)
    return patch("squid_pet.config.get",
                 side_effect=lambda k, default=None: defaults.get(k, default))


def test_compute_fires_approval_needed_from_claude_session(tmp_claude_dir):
    """Realistic case: Claude has genuinely gone idle (no live shell/file/
    streaming evidence -- exactly what the underlying cascade sees while
    a permission_prompt wait is actually in effect) and a
    flag is present -> approval_needed fires."""
    (tmp_claude_dir / "sess-xyz").write_text("permission_prompt")

    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="idle", message="x")
    with patch.object(watcher, "_fire_approval_notification") as mock_notify, \
         _patched_config():
        st = sm.compute()

    assert st.state == "approval_needed", (
        f"Claude session awaiting_input flag should fire approval_needed; "
        f"got {st.state!r}, reason={st.state_reason!r}"
    )
    assert "sess-xyz" in st.state_reason
    assert "claude" in st.state_reason.lower()
    # Pink-2026-08-26 regression: notification must name "Claude Code",
    # not the previously-hardcoded legacy agent (caught via live testing).
    # source_label defaults to "Claude Code" now that it's the only caller.
    pending = mock_notify.call_args.kwargs["still_pending"]
    mock_notify.assert_called_once_with("your turn", "Glass", still_pending=pending)
    assert pending()


def test_compute_notify_false_suppresses_notification_but_still_reports_state(tmp_claude_dir):
    """Pink-2026-08-27i: compute(notify=False) is StateMachine's own
    structural opt-out for read-only diagnostic callers (squid why),
    replacing an earlier unittest.mock.patch("_fire_approval_notification")
    reaching into the module from production code (__main__._run_why).
    The state/state_reason report must be unaffected -- only the real OS
    notification call is skipped."""
    (tmp_claude_dir / "sess-xyz").write_text("permission_prompt")

    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="idle", message="x")
    with patch.object(watcher, "_fire_approval_notification") as mock_notify, \
         _patched_config():
        st = sm.compute(notify=False)

    assert st.state == "approval_needed"
    assert "sess-xyz" in st.state_reason
    mock_notify.assert_not_called()


def test_compute_does_not_fire_without_a_claude_flag(tmp_claude_dir):
    """Sanity: an otherwise-quiet cascade must not spontaneously fire
    approval_needed just because the (empty) Claude dir exists."""
    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="idle", message="x")
    with _patched_config():
        st = sm.compute()
    assert st.state != "approval_needed"


# ── Regression: 2026-08-27 -- stale flag while genuinely active ────────
# Real bug caught via live use: Claude Code resumed working (a permission
# was granted some other way, or an agentic task continued unattended)
# WITHOUT the user ever submitting a fresh top-level prompt, so
# UserPromptSubmit never fired to clear the flag -- Squid kept showing
# "your turn" and re-firing the OS notification while Claude was visibly,
# actively working. Fix: our own real activity signal (working/thinking)
# self-heals the stale flag.
def _backdate(path, seconds):
    """Set a flag file's mtime `seconds` into the past, past self-heal's
    minimum-age grace window, so it reads as genuinely stale rather than
    just-written."""
    import os
    old = time.time() - seconds
    os.utime(path, (old, old))


#
# 2026-09-30: "working/thinking" is only the gate now -- the proof is a
# Bash-tool call that started AFTER the flag was raised (see the regression
# block at the end of this file for why activity alone was not proof).
def _new_tool_call_after_flag():
    """Evidence seam: a Bash-tool call started just now, after the flag."""
    return patch.object(watcher, "claude_newest_tool_shell_start",
                        side_effect=lambda procs: time.time())


def test_stale_flag_self_heals_when_genuinely_working(tmp_claude_dir):
    flag = tmp_claude_dir / "sess-stale"
    flag.write_text("permission_prompt")
    _backdate(flag, watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 1)

    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="working", message="x")
    with patch.object(watcher, "_fire_approval_notification") as mock_notify, \
         patch.object(watcher, "find_claude_code_processes", return_value=["proc1"]), \
         _new_tool_call_after_flag(), \
         _patched_config():
        st = sm.compute()

    assert st.state == "working", (
        f"genuine current activity must win over a stale flag; got {st.state!r}"
    )
    assert not flag.exists(), "the stale flag should be deleted, not just hidden"
    mock_notify.assert_not_called()


def test_stale_flag_self_heals_when_thinking(tmp_claude_dir):
    """Same self-heal, but with the cascade on 'thinking' (streaming) rather
    than 'working' -- the gate accepts either; the proof is the same new
    tool call (streaming alone never is: see the 2026-09-30 block)."""
    flag = tmp_claude_dir / "sess-stale-2"
    flag.write_text("permission_prompt")
    _backdate(flag, watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 1)

    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="thinking", message="x")
    with patch.object(watcher, "_fire_approval_notification"), \
         patch.object(watcher, "find_claude_code_processes", return_value=["proc1"]), \
         _new_tool_call_after_flag(), \
         _patched_config():
        st = sm.compute()

    assert st.state == "thinking"
    assert not flag.exists()


# ── Regression: 2026-08-31 -- self-heal ate a flag on the SAME tick it
# was written -- caught live during a demo: Squid never showed
# approval_needed at all (not even for one tick) for a genuinely-pending
# AskUserQuestion prompt, because Claude Code was still visibly
# streaming/active the instant the flag appeared, so self-heal reaped it
# before the approval-needed block ever got to see it. Self-heal exists
# to clear a flag stuck behind ongoing work Pink is actively watching --
# not to reap something raised a moment ago -- so a fresh flag must
# survive at least SELF_HEAL_MIN_FLAG_AGE_SEC before self-heal may act.
def test_fresh_flag_survives_self_heal_and_fires_approval_needed(tmp_claude_dir):
    flag = tmp_claude_dir / "sess-brand-new"
    flag.write_text("permission_prompt")  # mtime == now, well under the grace window

    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="thinking", message="x")
    with patch.object(watcher, "_fire_approval_notification") as mock_notify, \
         patch.object(watcher, "find_claude_code_processes", return_value=["proc1"]), \
         _patched_config():
        st = sm.compute()

    assert flag.exists(), (
        "a flag written this tick must not be self-healed away before "
        "it's ever had a chance to be seen"
    )
    assert st.state == "approval_needed", (
        f"a freshly-raised prompt must fire approval_needed on its first "
        f"tick, even if Claude Code still reads as working/thinking; "
        f"got {st.state!r}"
    )
    mock_notify.assert_called_once()


# ── Regression: 2026-08-30 -- self-heal ate a DIFFERENT session's
# genuinely-pending approval -- Pink caught live: session A asked for a
# decision and was waiting, but session B (this very conversation) was
# actively working, so self-heal's aggregate "any Claude Code activity
# means every pending wait is resolved" cleared session A's flag before
# approval_needed ever got a chance to fire. Self-heal's whole premise
# ("you're actively watching it work") only holds for a single active
# session -- with 2+ Claude Code processes alive there's no way to tell
# whose activity is whose (no PID in the hook payload), so it must not
# fire at all in that case; let the real removal paths (UserPromptSubmit,
# SessionEnd, stale-timeout, manual "calm Squid") handle it instead.
def test_stale_flag_does_not_self_heal_with_multiple_sessions_active(tmp_claude_dir):
    flag = tmp_claude_dir / "sess-other-session-waiting"
    flag.write_text("permission_prompt")
    # Past the too-fresh guard, and with new-tool-call evidence present, so
    # it is the multi-session guard alone that keeps the flag.
    _backdate(flag, watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 1)

    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="working", message="x")
    with patch.object(watcher, "_fire_approval_notification") as mock_notify, \
         patch.object(watcher, "find_claude_code_processes",
                       return_value=["proc-a", "proc-b"]), \
         _new_tool_call_after_flag(), \
         _patched_config():
        st = sm.compute()

    assert flag.exists(), (
        "with 2+ Claude Code processes alive, self-heal can't tell whose "
        "activity resolved whose wait -- must not clear another "
        "session's flag"
    )
    assert st.state == "approval_needed", (
        f"the genuinely-pending session must still get its attention "
        f"alert; got {st.state!r}"
    )
    mock_notify.assert_called_once()


def test_flag_written_after_going_idle_still_fires_normally(tmp_claude_dir):
    """Self-heal must not be overzealous: a flag that shows up while the
    cascade is genuinely idle (the realistic case) must still fire --
    only 'working'/'thinking' ticks self-heal."""
    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="idle", message="x")

    with patch.object(watcher, "_fire_approval_notification"), \
         _patched_config():
        # Tick 1: genuinely idle, no flag yet.
        st1 = sm.compute()
        assert st1.state == "idle"

        # Tick 2: a flag now appears (Notification hook fired between ticks).
        (tmp_claude_dir / "sess-fresh").write_text("permission_prompt")
        st2 = sm.compute()

    assert st2.state == "approval_needed"


def test_approval_alert_disabled_suppresses_claude_signal_too(tmp_claude_dir):
    """approval_alert_enabled=False must silence the Claude direct
    signal -- the one kill switch for the whole mechanism."""
    (tmp_claude_dir / "sess-xyz").write_text("permission_prompt")
    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="working", message="x")
    with _patched_config(approval_alert_enabled=False):
        st = sm.compute()
    assert st.state != "approval_needed"


# ── snooze_all_awaiting_now() / count_currently_waving_sessions() ──────

def test_count_currently_waving_sessions(tmp_claude_dir):
    (tmp_claude_dir / "sess-1").write_text("permission_prompt")
    (tmp_claude_dir / "sess-2").write_text("permission_prompt")
    assert watcher.count_currently_waving_sessions() == 2


def test_snooze_all_awaiting_now_snoozes_claude_sessions(tmp_claude_dir):
    (tmp_claude_dir / "sess-1").write_text("permission_prompt")
    # First establish eligibility (birth time recorded).
    assert watcher.count_currently_waving_sessions() == 1
    n = watcher.snooze_all_awaiting_now()
    assert n == 1
    # Now snoozed -- no longer eligible even though the flag file
    # is still on disk ("seen it, deferred" semantics).
    assert watcher.count_currently_waving_sessions() == 0


def test_snooze_all_awaiting_now_zero_when_nothing_waving(tmp_claude_dir):
    assert watcher.snooze_all_awaiting_now() == 0


# ── Regression: 2026-08-26 "endless notifications" bug ─────────────────
# Found via live testing: compute()'s agent_idle_seconds bookkeeping used to
# unconditionally reset self._approval_alert_fired = False whenever the
# PRE-override cascade state (from _compute_inner()) was in
# _AGENT_ACTIVE_STATES -- which is exactly the situation approval_needed
# fires in. That reset ran on every single tick, re-arming the "fire
# once" latch and causing a real macOS notification to fire once per
# second, forever, for as long as the flag stayed present. The correct
# reset already existed (fired_reason is None -> latch resets) and needed
# nothing added; the fix was deleting the premature one.
#
# Uses state="idle" here (not "working") -- 2026-08-27's separate
# stale-flag self-heal fix means "working"/"thinking" (plus, since
# 2026-09-30, a tool call started after the flag) can clear the flag
# before the approval block even runs, which no longer exercises this
# specific historical bug. "idle"
# (the realistic state while genuinely awaiting input) still does.

def test_notification_fires_only_once_across_many_ticks_while_flag_persists(tmp_claude_dir):
    """The exact bug: a live awaiting_input flag, ticked repeatedly while
    genuinely idle, must fire the OS notification exactly once, not once
    per tick."""
    (tmp_claude_dir / "sess-persistent").write_text("permission_prompt")

    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="idle", message="x")

    with patch.object(watcher, "_fire_approval_notification") as mock_notify, \
         _patched_config():
        states = [sm.compute() for _ in range(10)]

    assert all(st.state == "approval_needed" for st in states)
    assert mock_notify.call_count == 1, (
        f"notification must fire exactly once across 10 ticks with the "
        f"flag continuously present; fired {mock_notify.call_count} times"
    )


def test_notification_refires_after_flag_disappears_and_reappears(tmp_claude_dir):
    """Distinguishing the fix from a blunt 'never fire twice' latch: a
    genuinely NEW wait (user replied, then a fresh prompt/permission
    wait) must still notify again."""
    flag = tmp_claude_dir / "sess-cycle"
    flag.write_text("permission_prompt")

    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="idle", message="x")

    with patch.object(watcher, "_fire_approval_notification") as mock_notify, \
         _patched_config():
        st1 = sm.compute()
        assert st1.state == "approval_needed"
        assert mock_notify.call_count == 1

        flag.unlink()  # user replied -- UserPromptSubmit hook removed it
        st2 = sm.compute()
        assert st2.state != "approval_needed"

        flag.write_text("permission_prompt")  # a fresh wait
        st3 = sm.compute()
        assert st3.state == "approval_needed"
        assert mock_notify.call_count == 2, "must notify again for the new wait"


def test_state_reverts_once_flag_removed_between_ticks(tmp_claude_dir):
    """Simulates the UserPromptSubmit hook clearing the flag mid-session:
    the very next tick must fall out of approval_needed, no lingering."""
    flag = tmp_claude_dir / "sess-reply"
    flag.write_text("permission_prompt")

    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="idle", message="x")

    with patch.object(watcher, "_fire_approval_notification"), \
         _patched_config():
        assert sm.compute().state == "approval_needed"
        flag.unlink()
        st = sm.compute()

    assert st.state != "approval_needed", \
        "state must revert immediately once the flag is gone (next tick)"


# ── round 3 (review #3): a NEW add to a still-present flag re-arms ────────
# The flag is an owner set, so a second prompt in the same session (a helper's,
# or the parent's next one) no longer makes the file vanish and reappear -- it
# ADDS a line and advances the mtime. The snooze and the one-shot OS alert
# must re-arm on that advance, not only on a vanish/reappear. A partial clear
# never advances the mtime, so it must not re-alert.
def _set_mtime(path, t):
    import os
    os.utime(path, (t, t))


def test_mtime_advance_rearms_a_snoozed_session(tmp_claude_dir):
    flag = tmp_claude_dir / "sess-adv"
    flag.write_text("permission_prompt\nparent 1")
    now = time.time()
    _set_mtime(flag, now - 300)
    with patch("time.time", return_value=now):
        assert watcher.filter_eligible_claude_sessions(["sess-adv"]) == ["sess-adv"]
    later = now + watcher._CLAUDE_SESSION_SNOOZE_SEC + 1
    with patch("time.time", return_value=later):
        assert watcher.filter_eligible_claude_sessions(["sess-adv"]) == []
        _set_mtime(flag, now - 200)  # a new owner was added
        assert watcher.filter_eligible_claude_sessions(["sess-adv"]) == ["sess-adv"]


def test_unchanged_or_older_mtime_does_not_rearm(tmp_claude_dir):
    flag = tmp_claude_dir / "sess-part"
    flag.write_text("permission_prompt\nparent 1")
    now = time.time()
    _set_mtime(flag, now - 300)
    with patch("time.time", return_value=now):
        watcher.filter_eligible_claude_sessions(["sess-part"])
    later = now + watcher._CLAUDE_SESSION_SNOOZE_SEC + 1
    with patch("time.time", return_value=later):
        assert watcher.filter_eligible_claude_sessions(["sess-part"]) == []
        _set_mtime(flag, now - 400)  # partial clear: newest owner removed
        assert watcher.filter_eligible_claude_sessions(["sess-part"]) == []
        _set_mtime(flag, now - 300)  # back up to, but not past, the max seen
        assert watcher.filter_eligible_claude_sessions(["sess-part"]) == []


def test_manual_snooze_holds_until_a_new_add(tmp_claude_dir):
    flag = tmp_claude_dir / "sess-calm"
    flag.write_text("permission_prompt\nparent 1")
    _set_mtime(flag, time.time() - 60)
    watcher.snooze_all_awaiting_now()
    assert watcher.count_currently_waving_sessions() == 0
    assert watcher.count_currently_waving_sessions() == 0
    _set_mtime(flag, time.time())
    assert watcher.count_currently_waving_sessions() == 1


def test_os_alert_refires_when_a_new_owner_is_added(tmp_claude_dir):
    flag = tmp_claude_dir / "sess-two"
    flag.write_text("permission_prompt\nparent 1")
    _set_mtime(flag, time.time() - 60)
    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="idle", message="x")
    with patch.object(watcher, "_fire_approval_notification") as mock_notify, \
         _patched_config():
        for _ in range(3):
            assert sm.compute().state == "approval_needed"
        assert mock_notify.call_count == 1
        _set_mtime(flag, time.time() - 30)  # a helper's prompt was added
        assert sm.compute().state == "approval_needed"
        assert mock_notify.call_count == 2, "a new prompt must alert again"
        for _ in range(3):
            sm.compute()
        assert mock_notify.call_count == 2


def test_os_alert_does_not_refire_on_a_partial_clear(tmp_claude_dir):
    flag = tmp_claude_dir / "sess-pc"
    flag.write_text("permission_prompt\nparent 1\nagent:a 2")
    _set_mtime(flag, time.time() - 60)
    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state="idle", message="x")
    with patch.object(watcher, "_fire_approval_notification") as mock_notify, \
         _patched_config():
        sm.compute()
        flag.write_text("permission_prompt\nparent 1")
        _set_mtime(flag, time.time() - 90)
        sm.compute()
        sm.compute()
    assert mock_notify.call_count == 1


# ── round 3 (review #4): self-heal vs the owner set ────────────────────────
def _heal_tick(state="working", procs=("proc1",)):
    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(state=state, message="x")
    with patch.object(watcher, "_fire_approval_notification"), \
         patch.object(watcher, "find_claude_code_processes",
                      return_value=list(procs)), \
         _new_tool_call_after_flag(), \
         _patched_config():
        return sm.compute()


def test_self_heal_never_deletes_a_flag_with_several_owners(tmp_claude_dir):
    """Two owners = two independent pending prompts (the parent's and a
    helper's). The session looking busy says nothing about BOTH of them."""
    flag = tmp_claude_dir / "sess-multi"
    flag.write_text("permission_prompt\nparent 1.0\nagent:abc 2.0")
    _backdate(flag, watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 5)
    st = _heal_tick()
    assert flag.exists()
    assert st.state == "approval_needed"


def test_self_heal_still_clears_a_single_owner_flag(tmp_claude_dir):
    flag = tmp_claude_dir / "sess-single"
    flag.write_text("permission_prompt\nagent:abc 2.0")
    _backdate(flag, watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 5)
    assert _heal_tick().state == "working"
    assert not flag.exists()


def test_self_heal_skips_a_tick_while_the_hook_holds_the_lock(tmp_claude_dir):
    import fcntl
    flag = tmp_claude_dir / "sess-locked"
    flag.write_text("permission_prompt\nparent 1.0")
    _backdate(flag, watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 5)
    with open(tmp_claude_dir / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _heal_tick()
        assert flag.exists(), "self-heal must not delete under the hook's feet"
    _heal_tick()
    assert not flag.exists()


def test_self_heal_rechecks_age_after_taking_the_lock(tmp_claude_dir, monkeypatch):
    """A prompt added while self-heal waited for the lock makes the flag
    fresh again -- it must survive, like any freshly raised flag."""
    import fcntl
    import os
    flag = tmp_claude_dir / "sess-raced"
    flag.write_text("permission_prompt\nparent 1.0")
    _backdate(flag, watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 5)
    real_flock = fcntl.flock

    def flock_then_add(fd, op):
        os.utime(flag)  # the hook's add lands just before we get the lock
        return real_flock(fd, op)
    monkeypatch.setattr(watcher.fcntl, "flock", flock_then_add)
    _heal_tick()
    assert flag.exists()


# ── round 4 #2: the stale sweep takes the hook's lock before deleting ─────
# The hook re-raises a flag with a read-modify-write under its flock; a stale
# eviction that unlinked without the lock could delete a flag the hook had
# just re-raised (lost wave). Same own-lock-without-waiting pattern as the
# self-heal: contended -> skip this tick; acquired -> re-check, then delete.
def _make_stale(path):
    import os
    old = time.time() - watcher.CLAUDE_AWAITING_INPUT_STALE_SEC - 60
    os.utime(path, (old, old))


def test_stale_eviction_waits_for_the_hooks_lock(tmp_claude_dir):
    import fcntl
    flag = tmp_claude_dir / "sess-stale-locked"
    flag.write_text("permission_prompt\nparent 1.0")
    _make_stale(flag)
    with open(tmp_claude_dir / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert watcher.claude_sessions_awaiting_input() == []
        assert flag.exists(), "must not delete under the hook's feet"
    assert watcher.claude_sessions_awaiting_input() == []
    assert not flag.exists(), "the next tick cleans it up"


def test_stale_eviction_rechecks_the_mtime_under_the_lock(tmp_claude_dir,
                                                           monkeypatch):
    """A hook re-raise that lands while the sweep waits for the lock makes
    the flag fresh: it must survive AND be reported as live."""
    import fcntl
    import os
    flag = tmp_claude_dir / "sess-stale-raced"
    flag.write_text("permission_prompt\nparent 1.0")
    _make_stale(flag)
    real_flock = fcntl.flock

    def flock_then_raise(fd, op):
        if op & fcntl.LOCK_EX:
            os.utime(flag)  # the hook's re-raise lands just before we lock
        return real_flock(fd, op)
    monkeypatch.setattr(watcher.fcntl, "flock", flock_then_raise)
    assert watcher.claude_sessions_awaiting_input() == ["sess-stale-raced"]
    assert flag.exists()


def test_stale_eviction_keeps_a_flag_with_a_live_owner_line(tmp_claude_dir):
    """Belt and braces: the hook stamps the mtime from the newest owner, but a
    flag whose mtime is stale while an owner line is fresh is not deleted."""
    flag = tmp_claude_dir / "sess-stale-owner"
    flag.write_text(f"permission_prompt\nagent:abc {time.time() - 5:.6f}")
    _make_stale(flag)
    watcher.claude_sessions_awaiting_input()
    assert flag.exists()


def test_other_flag_dirs_still_evict_without_a_lock(tmp_path):
    """Only the awaiting-input dir has a hook lock; claude_finished/ and the
    rest keep the plain unlink."""
    import os
    d = tmp_path / "plain"
    d.mkdir()
    f = d / "sess-x"
    f.write_text("stop")
    old = time.time() - 100
    os.utime(f, (old, old))
    assert watcher._scan_session_flag_dir(str(d), 50) == []
    assert not f.exists()


# ── round 4 #0: the pending-correlation sweep ────────────────────────────
# PermissionRequest's attribution hints live in their own dir. SessionEnd
# removes a session's file; this sweep bounds the leak from a crash / missing
# SessionEnd. It deletes EVERY entry (dotfile tmps included) untouched for
# CLAUDE_PERMISSION_PENDING_STALE_SEC, under the hook's lock.
@pytest.fixture
def tmp_pending_dir(tmp_path, monkeypatch, tmp_claude_dir):
    d = tmp_path / "claude_permission_pending"
    d.mkdir()
    monkeypatch.setattr(watcher, "CLAUDE_PERMISSION_PENDING_DIR", str(d))
    monkeypatch.setattr(watcher, "_CLAUDE_PENDING_LAST_SWEEP", 0.0)
    return d


def _age(path, seconds):
    import os
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_pending_sweep_removes_an_old_entry_without_session_end(tmp_pending_dir):
    old = tmp_pending_dir / "sess-gone"
    old.write_text('{"agent_id": null, "tool_use_id": null, "ts": 1}\n')
    _age(old, watcher.CLAUDE_PERMISSION_PENDING_STALE_SEC + 10)
    orphan_tmp = tmp_pending_dir / ".sess-gone.123.tmp"
    orphan_tmp.write_text("x")
    _age(orphan_tmp, watcher.CLAUDE_PERMISSION_PENDING_STALE_SEC + 10)
    fresh = tmp_pending_dir / "sess-live"
    fresh.write_text('{"agent_id": null, "tool_use_id": null, "ts": 1}\n')
    assert watcher.sweep_claude_permission_pending() == 2
    assert not old.exists() and not orphan_tmp.exists()
    assert fresh.exists()


def test_pending_sweep_skips_while_the_hook_holds_the_lock(tmp_pending_dir,
                                                           tmp_claude_dir):
    import fcntl
    old = tmp_pending_dir / "sess-gone"
    old.write_text("x")
    _age(old, watcher.CLAUDE_PERMISSION_PENDING_STALE_SEC + 10)
    with open(tmp_claude_dir / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert watcher.sweep_claude_permission_pending() is None
        assert old.exists()
    assert watcher.sweep_claude_permission_pending() == 1
    assert not old.exists()


def test_pending_sweep_is_throttled_off_the_1hz_scan(tmp_pending_dir):
    """The awaiting-input scan runs every tick; the pending sweep piggybacks
    on it at most once per _CLAUDE_PENDING_SWEEP_INTERVAL_SEC (no per-tick
    listdir of a second dir)."""
    calls = []
    with patch.object(watcher, "sweep_claude_permission_pending",
                      side_effect=lambda now=None: calls.append(now) or 0):
        t0 = 1_000_000.0
        with patch("time.time", return_value=t0):
            watcher.claude_sessions_awaiting_input()
            watcher.claude_sessions_awaiting_input()
        with patch("time.time",
                   return_value=t0 + watcher._CLAUDE_PENDING_SWEEP_INTERVAL_SEC + 1):
            watcher.claude_sessions_awaiting_input()
    assert len(calls) == 2


def test_pending_sweep_throttle_advances_only_after_a_completed_pass(
        tmp_pending_dir, tmp_claude_dir):
    """Codex round 4 P3: the throttle used to be stamped BEFORE the lock was
    tried, so a pass skipped because a hook held the lock still deferred the
    next attempt a full interval -- under sustained contention, cleanup could
    slip indefinitely past the 5x margin under the 300s staleness bound. The
    stamp now moves only when a pass actually ran; a skipped pass is retried
    on the very next tick."""
    import fcntl
    old = tmp_pending_dir / "sess-gone"
    old.write_text("x")
    _age(old, watcher.CLAUDE_PERMISSION_PENDING_STALE_SEC + 10)
    interval = watcher._CLAUDE_PENDING_SWEEP_INTERVAL_SEC
    t0 = time.time()
    with open(tmp_claude_dir / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for k in range(4):  # several sweep intervals, lock busy throughout
            watcher._maybe_sweep_claude_permission_pending(t0 + k * interval)
            assert watcher._CLAUDE_PENDING_LAST_SWEEP == 0.0
            assert old.exists()
    # Lock free: the very next tick (1s later, well inside an interval) sweeps.
    watcher._maybe_sweep_claude_permission_pending(t0 + 3 * interval + 1)
    assert not old.exists()
    assert watcher._CLAUDE_PENDING_LAST_SWEEP == t0 + 3 * interval + 1
    # ...and the throttle is back in force after that completed pass.
    old.write_text("x")
    _age(old, watcher.CLAUDE_PERMISSION_PENDING_STALE_SEC + 10)
    watcher._maybe_sweep_claude_permission_pending(t0 + 3 * interval + 2)
    assert old.exists()


def test_pending_sweep_reports_a_skipped_pass_as_none(tmp_pending_dir,
                                                     tmp_claude_dir):
    """None = the pass did not run (lock busy) -- distinct from 0 = it ran and
    found nothing stale -- so the throttle can tell the two apart."""
    import fcntl
    (tmp_pending_dir / "sess-live").write_text("x")
    assert watcher.sweep_claude_permission_pending() == 0
    with open(tmp_claude_dir / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert watcher.sweep_claude_permission_pending() is None


def test_pending_sweep_with_no_dir_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "CLAUDE_PERMISSION_PENDING_DIR",
                        str(tmp_path / "missing"))
    assert watcher.sweep_claude_permission_pending() == 0


# ── Regression: 2026-09-30 -- self-heal ate a genuinely pending prompt ──────
# Caught live (twice in one day): Claude Code raised a permission prompt (and,
# separately, an AskUserQuestion) during a long turn whose EARLIER background
# tool calls were still running -- detached test runs and `while kill -0 ...;
# do sleep` wait loops. Those children kept the cascade on "working" ("shell
# child active (claude_code)"), and self-heal read that as "the wait is over":
# SELF_HEAL_MIN_FLAG_AGE_SEC after the Notification wrote the flag, it was
# deleted, so the OS alert fired but the flag-wave vanished within seconds
# (the hook log showed the next PostToolUse finding "no flag" although no hook
# had cleared it). Work that PREDATES the flag says nothing about whether the
# prompt was answered; only a NEW Bash-tool call, started after the flag was
# raised, does -- Claude is blocked at the prompt until it is answered.
class _FakeProc:
    """Just enough of psutil.Process for claude_newest_tool_shell_start."""

    def __init__(self, name, created, cmdline=(), children=()):
        self._name = name
        self._created = created
        self._cmdline = list(cmdline)
        self._children = list(children)

    def name(self):
        return self._name

    def create_time(self):
        return self._created

    def cmdline(self):
        return list(self._cmdline)

    def children(self, recursive=False):
        if not recursive:
            return list(self._children)
        out = []
        for ch in self._children:
            out.append(ch)
            out.extend(ch.children(recursive=True))
        return out


def _bash_tool_wrapper(created, cmd="pytest -q", children=()):
    """A Claude Code Bash-tool wrapper shell, as seen live: the user's shell
    sourcing Claude's shell snapshot, then eval'ing the command."""
    script = ("source /tmp/fake-home/.claude/shell-snapshots/snapshot-zsh-1-x.sh "
              "2>/dev/null || true && setopt NO_EXTENDED_GLOB && "
              f"eval '{cmd}' < /dev/null && pwd -P >| /tmp/claude-x-cwd")
    return _FakeProc("zsh", created, ["/bin/zsh", "-c", script], children)


def _claude(*children, created=1.0):
    return _FakeProc("claude", created, ["claude"], children)


def _pending_flag(tmp_claude_dir, age, first_line="permission_prompt",
                  sid="sess-alice"):
    flag = tmp_claude_dir / sid
    flag.write_text(f"{first_line}\nparent {time.time() - age:.6f}")
    _backdate(flag, age)
    return flag


def _background_work_started(seconds_ago):
    """A background test run + wait loop started by an EARLIER tool call:
    the wrapper shell and its long-running grandchildren (including a
    `sleep` / forked subshell re-spawned every few seconds -- brand new
    processes, but not new tool calls)."""
    now = time.time()
    forked_subshell = _bash_tool_wrapper(now - 1)  # same argv as its parent
    loop = _bash_tool_wrapper(
        now - seconds_ago, "while kill -0 123; do sleep 5; done",
        children=[forked_subshell, _FakeProc("sleep", now - 1, ["sleep", "5"])])
    tests = _bash_tool_wrapper(
        now - seconds_ago, "uv run pytest",
        children=[_FakeProc("python3", now - 2, ["python3", "-m", "pytest"])])
    return [loop, tests]


def _ticks(state, procs, n=3):
    sm = watcher.StateMachine()
    sm._compute_inner = lambda: watcher.PetState(
        state=state, message="x", state_reason="shell child active (claude_code)")
    with patch.object(watcher, "_fire_approval_notification") as mock_notify, \
         patch.object(watcher, "find_claude_code_processes",
                      return_value=list(procs)), \
         _patched_config():
        states = [sm.compute() for _ in range(n)]
    return states, mock_notify


@pytest.mark.parametrize("first_line", ["permission_prompt", "AskUserQuestion"])
@pytest.mark.parametrize("cascade", ["working", "thinking"])
def test_preexisting_background_shell_does_not_self_heal_a_pending_prompt(
        tmp_claude_dir, first_line, cascade):
    flag = _pending_flag(tmp_claude_dir, age=watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 7,
                         first_line=first_line)
    claude = _claude(*_background_work_started(seconds_ago=120))
    states, mock_notify = _ticks(cascade, [claude])
    assert flag.exists(), (
        "background work that predates the prompt is no proof it was answered")
    assert [s.state for s in states] == ["approval_needed"] * 3
    assert mock_notify.call_count == 1


def test_new_bash_tool_call_after_the_flag_self_heals_as_before(tmp_claude_dir):
    """Approval granted (or auto mode proceeding): Claude started a NEW Bash
    tool call after the prompt was raised -- a long command whose PostToolUse
    has not fired yet. The genuine case self-heal exists for."""
    flag = _pending_flag(tmp_claude_dir, age=watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 7)
    approved = _bash_tool_wrapper(time.time() - 2, "make test")
    claude = _claude(*_background_work_started(seconds_ago=120), approved)
    states, mock_notify = _ticks("working", [claude], n=1)
    assert not flag.exists()
    assert states[0].state == "working"
    mock_notify.assert_not_called()


def test_new_tool_call_evidence_does_not_override_the_multi_session_guard(
        tmp_claude_dir):
    flag = _pending_flag(tmp_claude_dir, age=watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 7)
    other = _claude(_bash_tool_wrapper(time.time() - 1))
    mine = _claude(_bash_tool_wrapper(time.time() - 1))
    states, _ = _ticks("working", [mine, other], n=1)
    assert flag.exists()
    assert states[0].state == "approval_needed"


def test_new_tool_call_evidence_does_not_override_the_too_fresh_guard(
        tmp_claude_dir):
    flag = _pending_flag(tmp_claude_dir, age=0.5)
    claude = _claude(_bash_tool_wrapper(time.time()))
    states, _ = _ticks("working", [claude], n=1)
    assert flag.exists()
    assert states[0].state == "approval_needed"


def test_self_heal_rechecks_the_evidence_against_the_mtime_under_the_lock(
        tmp_claude_dir, monkeypatch):
    """A prompt re-raised while self-heal waited for the lock moves the flag's
    mtime past the tool call it was about to cite as proof: keep it."""
    import fcntl
    import os
    flag = _pending_flag(tmp_claude_dir, age=30)
    tool_start = time.time() - 10
    claude = _claude(_bash_tool_wrapper(tool_start))
    real_flock = fcntl.flock

    def flock_then_reraise(fd, op):
        if op & fcntl.LOCK_EX:
            t = tool_start + 1  # newer than the tool call, still past min age
            os.utime(flag, (t, t))
        return real_flock(fd, op)
    monkeypatch.setattr(watcher.fcntl, "flock", flock_then_reraise)
    _ticks("working", [claude], n=1)
    assert flag.exists()


# ── claude_newest_tool_shell_start(): the evidence seam ────────────────────
def test_tool_start_is_the_newest_direct_bash_tool_wrapper():
    claude = _claude(_bash_tool_wrapper(100.0), _bash_tool_wrapper(250.0),
                     _bash_tool_wrapper(180.0))
    assert watcher.claude_newest_tool_shell_start([claude]) == 250.0


def test_tool_start_ignores_everything_that_is_not_a_new_tool_call():
    new = 500.0
    claude = _claude(
        # periodically re-spawned helper, MCP server, a hook run via sh -c
        _FakeProc("caffeinate", new, ["caffeinate", "-i", "-t", "300"]),
        _FakeProc("node", new, ["node", "/opt/mcp/server.js"]),
        _FakeProc("sh", new, ["/bin/sh", "-c", "python3 /opt/hook.py"]),
        # an old tool call whose descendants are brand new: a forked
        # subshell carries its parent's exact argv, but is not a direct child
        _bash_tool_wrapper(100.0, children=[
            _bash_tool_wrapper(new), _FakeProc("sleep", new, ["sleep", "5"])]),
    )
    assert watcher.claude_newest_tool_shell_start([claude]) == 100.0


def test_tool_start_is_none_without_evidence_or_on_errors():
    assert watcher.claude_newest_tool_shell_start([]) is None
    assert watcher.claude_newest_tool_shell_start([_claude()]) is None
    # Test doubles elsewhere hand self-heal plain strings: degrade to "no
    # evidence" (never heal), never raise.
    assert watcher.claude_newest_tool_shell_start(["proc1"]) is None

    class Gone(_FakeProc):
        def cmdline(self):
            raise ProcessLookupError

    claude = _claude(Gone("zsh", 900.0), _bash_tool_wrapper(50.0))
    assert watcher.claude_newest_tool_shell_start([claude]) == 50.0


def test_tool_start_accepts_the_snapshotless_eval_shape():
    script = "eval 'git status' < /dev/null && pwd -P >| /tmp/claude-x-cwd"
    claude = _claude(_FakeProc("bash", 42.0, ["/bin/bash", "-c", script]))
    assert watcher.claude_newest_tool_shell_start([claude]) == 42.0


def test_self_heal_evidence_read_is_throttled(tmp_claude_dir):
    """The evidence read is a full process-table sweep on macOS, and a flag
    kept pending by background work is now (correctly) NOT healed -- so it
    can stay pending for minutes. Re-read it at most once per
    SELF_HEAL_EVIDENCE_REFRESH_SEC, not every 1 Hz tick; a new tool call
    still heals the flag once the window lapses."""
    flag = _pending_flag(tmp_claude_dir, age=30)
    sid = flag.name
    sm = watcher.StateMachine()
    st = watcher.PetState(state="working", message="x")
    t0 = time.time()
    calls = []

    def evidence(procs):
        calls.append(procs)
        # First read: only the old background work. Later: a new tool call.
        return None if len(calls) == 1 else t0

    window = watcher.SELF_HEAL_EVIDENCE_REFRESH_SEC
    with patch.object(watcher, "find_claude_code_processes",
                      return_value=["proc1"]), \
         patch.object(watcher, "claude_newest_tool_shell_start",
                      side_effect=evidence):
        for dt in (0.0, 1.0, window - 0.5):
            assert sm._self_heal_stale_claude_flags(st, t0 + dt, [sid]) == [sid]
        assert len(calls) == 1, "one sweep per window, not one per tick"
        assert flag.exists()
        assert sm._self_heal_stale_claude_flags(st, t0 + window, [sid]) == []
    assert len(calls) == 2
    assert not flag.exists()


# ── Round 2 (2026-09-30 review P1-2): a HELPER's new Bash call is no proof ──
# A subagent's Bash wrapper is ALSO a direct child of the parent `claude`, so a
# background helper starting a command after the parent's prompt produced
# "a tool call newer than the flag" and self-heal unlinked the parent's
# genuinely pending prompt. A helper writes its tool_use entry to its own
# transcript (<projects>/<enc>/<sid>/subagents/agent-<id>.jsonl) BEFORE that
# Bash starts (measured live: 1.37 s earlier, the gap being PreToolUse hooks),
# so a helper transcript written since flag mtime - SELF_HEAL_HELPER_QUIET_SEC
# means the new tool call may be the helper's: keep the flag.
def _helper_transcript(projects_dir, sid, mtime, agent="agent-a1b2c3.jsonl"):
    sub = projects_dir / "-tmp-fake-home-proj" / sid / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / agent
    f.write_text("{}\n")
    os.utime(f, (mtime, mtime))
    return f


@pytest.mark.parametrize("first_line", ["permission_prompt", "AskUserQuestion"])
def test_helper_bash_started_after_the_parent_prompt_does_not_self_heal(
        tmp_claude_dir, tmp_projects_dir, first_line):
    flag = _pending_flag(tmp_claude_dir, age=watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 7,
                         first_line=first_line)
    now = time.time()
    # The helper wrote its tool_use, then its Bash started -- both after the
    # parent's prompt was raised.
    _helper_transcript(tmp_projects_dir, flag.name, now - 3)
    claude = _claude(*_background_work_started(seconds_ago=120),
                     _bash_tool_wrapper(now - 2, "uv run pytest -x"))
    states, mock_notify = _ticks("working", [claude])
    assert flag.exists(), "a helper's new tool call is no proof the PARENT's prompt was answered"
    assert [s.state for s in states] == ["approval_needed"] * 3
    assert mock_notify.call_count == 1


def test_helper_quiet_since_before_the_prompt_still_lets_a_new_tool_call_heal(
        tmp_claude_dir, tmp_projects_dir):
    """A helper that last wrote well before the prompt cannot own a tool call
    started after it: the parent's approved long command heals as before."""
    flag = _pending_flag(tmp_claude_dir, age=watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 7)
    mtime = os.stat(flag).st_mtime
    _helper_transcript(tmp_projects_dir, flag.name,
                       mtime - watcher.SELF_HEAL_HELPER_QUIET_SEC - 5)
    # Another session's helper, active right now, is irrelevant to this flag.
    _helper_transcript(tmp_projects_dir, "sess-other", time.time())
    claude = _claude(_bash_tool_wrapper(time.time() - 2, "make test"))
    states, _ = _ticks("working", [claude], n=1)
    assert not flag.exists()
    assert states[0].state == "working"


def test_helper_activity_within_the_tolerance_before_the_prompt_blocks_the_heal(
        tmp_claude_dir, tmp_projects_dir):
    """PreToolUse hooks / the auto-mode classifier run between a helper's
    tool_use write and its Bash starting, so a write slightly BEFORE the
    prompt can still belong to a Bash that started after it."""
    flag = _pending_flag(tmp_claude_dir, age=watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 7)
    mtime = os.stat(flag).st_mtime
    _helper_transcript(tmp_projects_dir, flag.name,
                       mtime - watcher.SELF_HEAL_HELPER_QUIET_SEC + 5)
    claude = _claude(_bash_tool_wrapper(time.time() - 2))
    states, _ = _ticks("working", [claude], n=1)
    assert flag.exists()
    assert states[0].state == "approval_needed"


def test_under_the_lock_the_helper_check_uses_the_locked_mtime(tmp_claude_dir):
    """A partial resolve can LOWER the flag's mtime (the hook keeps the newest
    remaining owner's time) between the pre-check and the lock: re-check the
    helper window against the mtime actually held under the lock."""
    now = time.time()
    flag = _pending_flag(tmp_claude_dir, age=100)
    mtime = os.stat(flag).st_mtime
    ok = mtime - watcher.SELF_HEAL_HELPER_QUIET_SEC - 1
    assert watcher._self_heal_unlink_single_owner_flag(
        str(flag), now, evidence_after=now - 1,
        helper_write=mtime - watcher.SELF_HEAL_HELPER_QUIET_SEC + 1) is False
    assert flag.exists()
    assert watcher._self_heal_unlink_single_owner_flag(
        str(flag), now, evidence_after=now - 1, helper_write=ok) is True
    assert not flag.exists()


def test_helper_read_follows_the_process_read_and_shares_its_throttle(
        tmp_claude_dir):
    """The helper read must come AFTER the process read it vets (a Bash in
    that snapshot already wrote its tool_use), and must not become a
    per-tick scan while a helper keeps the flag pending."""
    flag = _pending_flag(tmp_claude_dir, age=30)
    sid = flag.name
    sm = watcher.StateMachine()
    st = watcher.PetState(state="working", message="x")
    t0 = time.time()
    order = []
    helper_write = [t0]  # a helper active right now

    def procs_read(procs):
        order.append("procs")
        return t0

    def helper_read(session_id):
        order.append(("helper", session_id))
        return helper_write[0]

    window = watcher.SELF_HEAL_EVIDENCE_REFRESH_SEC
    with patch.object(watcher, "find_claude_code_processes",
                      return_value=["proc1"]), \
         patch.object(watcher, "claude_newest_tool_shell_start",
                      side_effect=procs_read), \
         patch.object(watcher, "claude_session_newest_helper_write",
                      side_effect=helper_read):
        for dt in (0.0, 1.0, window - 0.5):
            assert sm._self_heal_stale_claude_flags(st, t0 + dt, [sid]) == [sid]
        assert order == ["procs", ("helper", sid)], "one read of each per window"
        helper_write[0] = t0 - 3600  # the helper went quiet long ago
        assert sm._self_heal_stale_claude_flags(st, t0 + window, [sid]) == []
    assert order == ["procs", ("helper", sid)] * 2
    assert not flag.exists()


# ── claude_session_newest_helper_write(): the helper-activity seam ─────────
def test_newest_helper_write_is_the_newest_subagent_transcript_of_the_session(
        tmp_projects_dir):
    _helper_transcript(tmp_projects_dir, "sess-x", 100.0, "agent-a.jsonl")
    _helper_transcript(tmp_projects_dir, "sess-x", 300.0, "agent-b.jsonl")
    _helper_transcript(tmp_projects_dir, "sess-x", 900.0, "agent-b.meta.json")
    _helper_transcript(tmp_projects_dir, "sess-other", 999.0)
    assert watcher.claude_session_newest_helper_write("sess-x") == 300.0


def test_newest_helper_write_is_none_without_helpers(tmp_projects_dir):
    # No projects dir at all, then a project with no subagents for this session.
    assert watcher.claude_session_newest_helper_write("sess-x") is None
    (tmp_projects_dir / "-tmp-fake-home-proj").mkdir(parents=True)
    (tmp_projects_dir / "-tmp-fake-home-proj" / "sess-x.jsonl").write_text("{}\n")
    (tmp_projects_dir / "stray-file").write_text("")
    assert watcher.claude_session_newest_helper_write("sess-x") is None


def test_newest_helper_write_unreadable_means_do_not_heal(
        tmp_claude_dir, tmp_projects_dir):
    """Unlike the process evidence (failure = no evidence = no heal), a
    failed HELPER read must also block the heal: 'could not tell' is not
    'no helper'."""
    f = _helper_transcript(tmp_projects_dir, "sess-x", 100.0)
    sub = f.parent
    os.chmod(sub, 0)
    try:
        assert watcher.claude_session_newest_helper_write("sess-x") == float("inf")
    finally:
        os.chmod(sub, 0o755)

    flag = _pending_flag(tmp_claude_dir, age=watcher.SELF_HEAL_MIN_FLAG_AGE_SEC + 7)
    claude = _claude(_bash_tool_wrapper(time.time() - 2))
    with patch.object(watcher, "claude_session_newest_helper_write",
                      return_value=float("inf")):
        states, _ = _ticks("working", [claude], n=1)
    assert flag.exists()
    assert states[0].state == "approval_needed"
