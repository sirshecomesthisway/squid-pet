#!/usr/bin/env python3
"""
claude_pet_hook.py -- squid-pet's Claude Code hook receiver.

Wired into ~/.claude/settings.json under hooks.Notification,
hooks.UserPromptSubmit, hooks.SessionEnd, hooks.Stop, hooks.PreCompact,
hooks.PostCompact, hooks.PostToolUse, and hooks.PreToolUse (the last
matched to AskUserQuestion|ExitPlanMode), and hooks.StopFailure. Maintains
four per-session flag-file signals that watcher.py reads:
  - "awaiting input" (claude_sessions_awaiting_input()) -- mirrors what
    the legacy agent's own sitecustomize.py patch did via a PID-keyed
    flag directory, except keyed by session_id, since Claude Code hook
    payloads carry no PID.
  - "just finished" (claude_sessions_just_finished()) -- Pink-2026-08-27f:
    replaces the old busy->idle heuristic edge (ClaudeCodeDetector
    watching shell/file/transcript-mtime activity drop) as the
    "celebrate" trigger. That heuristic could -- and did, confirmed
    live -- flip mid-task during an ordinary >20s gap with no tool call,
    producing a false "finished with claude!" bubble while Claude was
    still actively working. The official Stop hook fires exactly when
    Claude finishes responding and hands control back, which is what
    "worth celebrating" actually means -- same fix pattern as the
    Notification-hook migration above, applied to a different signal.
  - "recapping" (claude_sessions_recapping()) -- Pink-2026-08-30: Pink
    noticed Squid flashing to a generic "thinking" with no explanation
    during a context compaction (/compact or auto-compact) and asked
    for it to be called out by name. PreCompact/PostCompact bracket the
    compaction exactly, unlike any of the busy signals above (a compact
    is pure summarization -- no tool calls, no file writes).
  - "failed" (claude_freshest_failure()) -- Pink-2026-09-16: drives the
    concerned/warning sprite. Claude Code fires StopFailure (NOT Stop)
    when a turn ends on an API error, carrying error_type; we record just
    that CATEGORY. The only inference-free "she is blocked by an error"
    signal -- Pink rejected guessing concerned from a stalled/silent turn.

Protocol:
  - Notification with notification_type == permission_prompt
    -> write <awaiting_input_dir>/<session_id>  (Claude is BLOCKED on you)
    idle_prompt is deliberately ignored -- see _HANDLED_NOTIFICATION_TYPES
  - PreToolUse with tool_name in {AskUserQuestion, ExitPlanMode}
    -> write <awaiting_input_dir>/<session_id>  (blocked on a human
    answer/approval that fires NO permission_prompt -- see
    _AWAITING_INPUT_TOOLS). The PostToolUse when you answer clears it.
  - UserPromptSubmit -> remove <awaiting_input_dir>/<session_id>  (you replied)
  - SessionEnd -> remove <awaiting_input_dir>/<session_id> and
    <recap_dir>/<session_id>  (session is gone)
  - Stop -> write <finished_dir>/<session_id>  (Claude just finished a turn)
  - StopFailure -> write <failed_dir>/<session_id> = error_type  (turn ended
    on an API error). Also closes the turn bracket; cleared on the next
    UserPromptSubmit (retry) or SessionEnd.
  - PreCompact -> write <recap_dir>/<session_id>  (compaction starting)
  - PostCompact -> remove <recap_dir>/<session_id>  (compaction done)

No message/transcript content is ever read or written for any of these
signals -- session_id and mtime only, matching this project's stated
privacy stance (see ClaudeCodeDetector's docstring on why transcript
content is never read). Stop's payload includes a last_assistant_message
field; PreCompact's includes custom_instructions; both are deliberately
ignored. PreCompact's trigger field ("manual" vs "auto") is also
ignored for now -- Squid shows the same "recapping" bubble either way.

Confirmed empirically (2026-08-25/26, live Claude Code 2.1.239): the
Notification payload for both permission_prompt and idle_prompt includes
session_id, transcript_path, cwd, prompt_id, hook_event_name, message,
notification_type. UserPromptSubmit/SessionEnd/Stop are assumed to carry
session_id too (documented as a field common to every hook event) but
were NOT independently re-verified against a live payload the same way
-- that's what the always-on log below is for: check
~/.squid-pet/claude_hook.log after a real prompt-submit/session-end/stop
to confirm this script actually saw and handled them, without needing
another dedicated diagnostic pass. Also unverified: whether a Task-tool
subagent's completion fires this same top-level Stop event in addition
to its own SubagentStop (which this script does not handle) -- if the
log ever shows Stop firing implausibly often during subagent-heavy
sessions, that's the first thing to check. PreCompact/PostCompact are
documented (see Claude Code hooks-guide.md) but not yet independently
verified against a live payload either -- same log-based confirmation
applies after a real /compact.

Never raises past main(), never blocks Claude Code, always exits 0 -- a
bug here must not be able to interfere with normal Claude Code use. This
script deliberately has ZERO dependencies beyond the stdlib and does NOT
import the squid_pet package (the hook's execution environment has no
guarantee squid_pet is on PYTHONPATH).

Testability: the base directory (normally ~/.squid-pet) is read from the
SQUID_PET_HOME env var if set, so tests can point it at a tmp_path
without touching the real ~/.squid-pet.
"""
from __future__ import annotations

import json
import os
import sys
import time

_SQUID_PET_HOME = os.environ.get(
    "SQUID_PET_HOME", os.path.join(os.path.expanduser("~"), ".squid-pet")
)
FLAG_DIR = os.path.join(_SQUID_PET_HOME, "claude_awaiting_input")
FINISHED_DIR = os.path.join(_SQUID_PET_HOME, "claude_finished")
RECAP_DIR = os.path.join(_SQUID_PET_HOME, "claude_recapping")
# Pink-2026-09-16: "the turn ended because of an API error". Claude Code
# fires StopFailure (NOT Stop) when a turn ends on a usage/rate limit,
# server overload, auth/billing failure, etc., carrying a machine-readable
# error_type. This is the only inference-free signal that she is BLOCKED by
# an error rather than merely quiet -- watcher.claude_freshest_failure()
# reads this dir to drive the concerned/warning sprite. Content-blind like
# every other flag: we record only the error_type CATEGORY, never message
# text. Cleared when the user reacts (UserPromptSubmit) or the session ends.
FAILED_DIR = os.path.join(_SQUID_PET_HOME, "claude_failed")
# Pink-2026-09-16: session_id -> controlling tty, so "take me there" can raise
# the EXACT terminal/app a signal came from. focus.py otherwise matches a
# session to its process by cwd, which is ambiguous when two sessions run in
# the same directory (one in Terminal, one in Cursor) -- the wrong-window bug
# where a failed Cursor turn's concerned double-click raised the Terminal
# session instead. The hook is a non-detached child of the session's `claude`
# process, so it shares that session's controlling terminal: an authoritative
# key, not a guess. A tty is a device number, not user content -- consistent
# with this script's content-blindness (see docs/PRIVACY.md).
TTY_DIR = os.path.join(_SQUID_PET_HOME, "claude_session_tty")
# Pink-2026-09-01: "a turn is in flight". UserPromptSubmit opens it, Stop
# closes it. Exists because ClaudeCodeDetector infers thinking from
# transcript mtime, and Claude Code only writes a transcript entry when a
# block COMPLETES -- an extended thinking stretch writes nothing, so past
# STREAMING_STALE_SEC Squid showed idle while the UI said "thinking some
# more" (measured: 29s wrongly-idle in one stretch). These two hooks
# bracket a turn exactly, with no inference. Content-blind like every
# other flag here: session_id and mtime only, never the prompt text.
TURN_ACTIVE_DIR = os.path.join(_SQUID_PET_HOME, "claude_turn_active")
LOG_PATH = os.path.join(_SQUID_PET_HOME, "claude_hook.log")
_LOG_MAX_BYTES = 200_000
_LOG_KEEP_LINES = 1000

# Pink-2026-09-01: idle_prompt REMOVED. It fires 60s after Claude hands
# control back and means only "it is your turn and you are not at the
# keyboard" -- nothing is blocked, nothing needs a decision. Pink got two
# banners in one sitting from it and asked for exactly this: "I don't need
# her to tell me what to do next, only to speak up when she needs me."
#
# This does NOT weaken the stepped-away case. A permission_prompt raised
# while you are away still waves and still fires the banner -- that is the
# alert worth walking back for. What is gone is the one that fires when
# nothing is waiting on you at all.
_HANDLED_NOTIFICATION_TYPES = frozenset({"permission_prompt"})

# Pink-2026-09-06: some "blocked on you" moments emit NO Notification at
# all -- AskUserQuestion (Claude is asking you to choose) and ExitPlanMode
# (Claude is waiting for you to approve a plan). The only thing they
# eventually fire is idle_prompt after ~60s, which we ignore. But they are
# TOOLS, so PreToolUse fires right before they block, carrying tool_name.
# Treat those exactly like a permission_prompt: raise the awaiting-input
# flag. Every OTHER tool's PreToolUse is Claude working, not waiting, and
# must be a no-op. The flag clears on its own: answering the question fires
# PostToolUse (already a remove-event), and DENIAL/exit is covered by Stop.
_AWAITING_INPUT_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Pink-2026-08-31: PostToolUse and Stop added after a confirmed stuck-wave
# bug. ANSWERING a permission prompt -- yes OR no -- fires no hook of any
# kind. Only UserPromptSubmit (you typed a new message) and SessionEnd (the
# session exited) were clearing the flag, so approving a prompt and letting
# Claude carry on left the flag set indefinitely: Squid waved for nine
# minutes at a session that was happily working, and because approval_needed
# takes PRIME over the whole cascade, every subsequent working/thinking state
# was masked behind it. Confirmed live in claude_hook.log, session 8ced357d:
# Notification WRITE permission_prompt -> Stop (so tools ran, i.e. it was
# approved) -> ... -> SessionEnd REMOVED, nine minutes later.
#
# The watcher has a self-heal for exactly this, but it is gated on
# len(find_claude_code_processes()) <= 1 (deliberately -- see its comment on
# the multi-session false-clear bug), and anyone running two sessions never
# gets it. These two events give a per-session signal that needs no such gate:
#
#   PostToolUse -- a tool actually executed. Proof the permission question
#                  was resolved and the session is no longer blocked on you.
#   Stop        -- the turn ended and control came back. Whatever it was
#                  waiting for, it is not waiting now. This is what covers
#                  DENIAL, where no tool ever runs and PostToolUse never
#                  fires.
#
# Neither can suppress a genuine wave: the next permission_prompt re-arms
# the flag on its own, whenever the session next actually blocks on you.
_REMOVE_ON_EVENTS = frozenset({
    "UserPromptSubmit", "SessionEnd", "PostToolUse", "Stop",
})

# Events handled by the SECOND dispatch chain (the one after the
# remove-on-events / Notification / PreToolUse chain). Kept here so the
# trailing UNKNOWN_EVENT guard doesn't mislabel them as unhandled.
_SECOND_CHAIN_EVENTS = frozenset({"Stop", "StopFailure", "PreCompact", "PostCompact"})

# Turn bracket: which events open a turn, and which close it.
_TURN_OPEN_EVENTS = frozenset({"UserPromptSubmit"})
# SessionEnd is crash safety -- a session killed mid-turn must not leave
# Squid thinking forever. StopFailure ends the turn too (it fires INSTEAD of
# Stop on an errored turn), so it must close the bracket as well -- otherwise
# turn_in_flight stays True and the stall path keeps painting "thinking" over
# the error.
_TURN_CLOSE_EVENTS = frozenset({"Stop", "StopFailure", "SessionEnd"})

# Events that mean the user has reacted to / moved past a prior failure, so a
# stuck concerned/warning flag should stand down. UserPromptSubmit = retried
# or typed something new; SessionEnd = the session is gone (crash safety).
_CLEAR_FAILED_EVENTS = frozenset({"UserPromptSubmit", "SessionEnd"})


def _truncate_log_if_large() -> None:
    """Keep the log bounded -- this runs for the lifetime of the user's
    Claude Code usage, so an unbounded append-only file is a real risk
    over months of use."""
    try:
        if os.path.getsize(LOG_PATH) <= _LOG_MAX_BYTES:
            return
        with open(LOG_PATH, "r") as f:
            lines = f.readlines()
        with open(LOG_PATH, "w") as f:
            f.writelines(lines[-_LOG_KEEP_LINES:])
    except OSError:
        pass


def _log(line: str) -> None:
    try:
        os.makedirs(_SQUID_PET_HOME, exist_ok=True)
        _truncate_log_if_large()
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.time():.0f} {line}\n")
    except Exception:
        pass


def _controlling_tty() -> str | None:
    """The controlling terminal of the Claude Code session that fired this
    hook -- the same /dev/ttysNNN the session's `claude` process reports and
    that Terminal.app exposes as `tty of tab`. This is the authoritative
    session->tty key: exact even when two sessions run in the same
    directory, where matching by cwd cannot tell them apart.

    Stdlib-only and best-effort (this script must never fail). Two ways,
    strongest-available first:

      1. OUR OWN controlling terminal (/dev/tty, then the standard fds).
         Works when the hook is a plain terminal child (Terminal.app), and
         is the cheap path.
      2. THE CLAUDE ANCESTOR's terminal, read from `ps`. Claude Code spawns
         hooks WITHOUT a controlling terminal of their own (verified live:
         a hook process shows `tty ??`), so (1) returns nothing there -- but
         the hook is still a descendant of exactly the session's `claude`
         process, which does have a tty. Walking the parent chain up to the
         first ancestor that has a real terminal recovers it, and because we
         start from THIS hook it can only reach the process tree that
         spawned us, i.e. the right session's.

    Returns None only if no ancestor has a terminal (a truly headless run),
    and the reader then falls back to the old cwd guess -- no worse than
    before."""
    own = _own_controlling_tty()
    if own:
        return own
    return _ancestor_tty_via_ps()


def _own_controlling_tty() -> str | None:
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
        try:
            return os.ttyname(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    for fd in (0, 1, 2):
        try:
            return os.ttyname(fd)
        except OSError:
            continue
    return None


def _ancestor_tty_via_ps() -> str | None:
    """First ancestor process (starting from this hook) that has a real
    controlling terminal, via a single `ps` snapshot walked in memory.

    One subprocess call, not one per hop -- this runs on every hook event,
    so it stays cheap. Any failure yields None (best-effort)."""
    import subprocess
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,tty="],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return None
    return _first_ancestor_tty(out, os.getpid())


def _first_ancestor_tty(ps_output: str, start_pid: int) -> str | None:
    """Walk `pid=,ppid=,tty=` ps output from start_pid up the parent chain,
    returning the first process that has a real controlling terminal.

    Pure (no subprocess) so the walk is unit-testable. Returns the tty in
    the /dev-prefixed form the session's process and Terminal.app both use."""
    parent: dict[int, int] = {}
    tty: dict[int, str] = {}
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid_i, ppid_i = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parent[pid_i] = ppid_i
        # A process with no controlling terminal prints "??"; a missing 3rd
        # field is treated the same.
        tty[pid_i] = parts[2] if len(parts) >= 3 else "??"
    pid = start_pid
    depth = 0
    while pid and pid > 1 and depth < 40:
        t = tty.get(pid)
        if t and t != "??":
            return t if t.startswith("/dev/") else "/dev/" + t
        pid = parent.get(pid, 0)
        depth += 1
    return None


def _record_session_tty(session_id: str) -> None:
    """Best-effort: remember which terminal this session lives in, refreshed
    on every event so it tracks a session that moves hosts. Never fails the
    hook; a missing tty simply leaves no entry."""
    tty = _controlling_tty()
    if not tty:
        return
    if not _ensure_dir(TTY_DIR, "tty"):
        return
    try:
        with open(os.path.join(TTY_DIR, session_id), "w") as f:
            f.write(tty)
    except Exception as e:
        _log(f"TTY {session_id} WRITE_FAILED {e!r}")


def _ensure_dir(path: str, event: str) -> bool:
    """mkdir -p, logging + returning False on failure so the caller can
    bail out before attempting a file op inside a dir that isn't there."""
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception as e:
        _log(f"{event} MKDIR_FAILED {e!r}")
        return False


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        _log(f"PARSE_ERROR {e!r}")
        return 0

    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")
    if not session_id:
        _log(f"{event} NO_SESSION_ID")
        return 0

    # Record this session's terminal before dispatching, so every signal a
    # session can raise ("take me there") is resolvable to the exact tab/app
    # it fired from, not guessed by cwd. Cleared on SessionEnd below.
    _record_session_tty(session_id)

    if event == "Notification":
        ntype = payload.get("notification_type", "")
        if not _ensure_dir(FLAG_DIR, event):
            return 0
        flag_path = os.path.join(FLAG_DIR, session_id)
        if ntype in _HANDLED_NOTIFICATION_TYPES:
            try:
                with open(flag_path, "w") as f:
                    f.write(ntype)
                _log(f"Notification {session_id} WRITE {ntype}")
            except Exception as e:
                _log(f"Notification {session_id} WRITE_FAILED {e!r}")
        else:
            _log(f"Notification {session_id} IGNORED {ntype!r}")
    elif event == "PreToolUse":
        tool = payload.get("tool_name", "")
        if tool in _AWAITING_INPUT_TOOLS:
            if not _ensure_dir(FLAG_DIR, event):
                return 0
            flag_path = os.path.join(FLAG_DIR, session_id)
            try:
                with open(flag_path, "w") as f:
                    f.write(tool)
                _log(f"PreToolUse {session_id} WRITE {tool}")
            except Exception as e:
                _log(f"PreToolUse {session_id} WRITE_FAILED {e!r}")
        else:
            _log(f"PreToolUse {session_id} NOOP {tool!r}")
    elif event in _REMOVE_ON_EVENTS:
        if not _ensure_dir(FLAG_DIR, event):
            return 0
        flag_path = os.path.join(FLAG_DIR, session_id)
        try:
            os.unlink(flag_path)
            _log(f"{event} {session_id} REMOVED")
        except FileNotFoundError:
            _log(f"{event} {session_id} NOOP (no flag)")
        except Exception as e:
            _log(f"{event} {session_id} REMOVE_FAILED {e!r}")
        if event == "SessionEnd":
            # Crash-safety only -- PostCompact is the normal way this
            # clears. A session ending mid-compact (rare) would otherwise
            # leave a stuck "recapping" flag for watcher.py's stale-sweep
            # to clean up 2h later instead of right away.
            try:
                os.unlink(os.path.join(RECAP_DIR, session_id))
                _log(f"{event} {session_id} RECAP_REMOVED")
            except (FileNotFoundError, OSError):
                pass
            try:
                os.unlink(os.path.join(TTY_DIR, session_id))
                _log(f"{event} {session_id} TTY_REMOVED")
            except (FileNotFoundError, OSError):
                pass
    if event == "Stop":
        if not _ensure_dir(FINISHED_DIR, event):
            return 0
        finished_path = os.path.join(FINISHED_DIR, session_id)
        try:
            with open(finished_path, "w") as f:
                f.write("stop")
            _log(f"Stop {session_id} WRITE")
        except Exception as e:
            _log(f"Stop {session_id} WRITE_FAILED {e!r}")
    elif event == "StopFailure":
        # Turn ended on an API error. Record the error_type CATEGORY only
        # (never message text) so the watcher can show the right concerned
        # reason/severity. Missing error_type still means "the turn failed" --
        # record it as "unknown" rather than dropping the signal.
        if not _ensure_dir(FAILED_DIR, event):
            return 0
        error_type = payload.get("error_type") or "unknown"
        failed_path = os.path.join(FAILED_DIR, session_id)
        try:
            with open(failed_path, "w") as f:
                f.write(str(error_type))
            _log(f"StopFailure {session_id} WRITE {error_type}")
        except Exception as e:
            _log(f"StopFailure {session_id} WRITE_FAILED {e!r}")
    elif event == "PreCompact":
        if not _ensure_dir(RECAP_DIR, event):
            return 0
        recap_path = os.path.join(RECAP_DIR, session_id)
        trigger = payload.get("trigger", "")
        try:
            with open(recap_path, "w") as f:
                f.write(trigger or "recap")
            _log(f"PreCompact {session_id} WRITE {trigger!r}")
        except Exception as e:
            _log(f"PreCompact {session_id} WRITE_FAILED {e!r}")
    elif event == "PostCompact":
        if not _ensure_dir(RECAP_DIR, event):
            return 0
        recap_path = os.path.join(RECAP_DIR, session_id)
        try:
            os.unlink(recap_path)
            _log(f"PostCompact {session_id} REMOVED")
        except FileNotFoundError:
            _log(f"PostCompact {session_id} NOOP (no flag)")
        except Exception as e:
            _log(f"PostCompact {session_id} REMOVE_FAILED {e!r}")
    if event in _CLEAR_FAILED_EVENTS:
        try:
            os.unlink(os.path.join(FAILED_DIR, session_id))
            _log(f"{event} {session_id} FAILED_CLEARED")
        except (FileNotFoundError, OSError):
            pass

    if event in _TURN_OPEN_EVENTS:
        if _ensure_dir(TURN_ACTIVE_DIR, event):
            try:
                with open(os.path.join(TURN_ACTIVE_DIR, session_id), "w") as f:
                    f.write("turn")
                _log(f"{event} {session_id} TURN_OPEN")
            except Exception as e:
                _log(f"{event} {session_id} TURN_OPEN_FAILED {e!r}")
    elif event in _TURN_CLOSE_EVENTS:
        try:
            os.unlink(os.path.join(TURN_ACTIVE_DIR, session_id))
            _log(f"{event} {session_id} TURN_CLOSE")
        except (FileNotFoundError, OSError):
            pass

    if (event not in _REMOVE_ON_EVENTS
            and event not in _SECOND_CHAIN_EVENTS
            and event not in ("Notification", "PreToolUse")):
        # Guarded against BOTH dispatch chains above: an event handled there
        # (PostToolUse, UserPromptSubmit, Stop, StopFailure, PreCompact, ...)
        # reaches here too, and without this check it would be logged as
        # UNKNOWN alongside its own successful handling -- misleading in
        # exactly the log you reach for when a flag looks stuck.
        _log(f"UNKNOWN_EVENT {event!r} {session_id}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
