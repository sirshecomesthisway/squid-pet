#!/usr/bin/env python3
"""
claude_pet_hook.py -- squid-pet's Claude Code hook receiver.

Wired into ~/.claude/settings.json under hooks.Notification,
hooks.PermissionRequest, hooks.PermissionDenied, hooks.UserPromptSubmit,
hooks.SessionEnd, hooks.Stop, hooks.PreCompact, hooks.PostCompact,
hooks.PostToolUse, hooks.PostToolUseFailure, hooks.PreToolUse (matched to
AskUserQuestion|ExitPlanMode), hooks.StopFailure, and hooks.SubagentStop.
PermissionRequest SHOULD be registered: it is the only event that says
WHOSE permission prompt is up (see "Established" below); without it every
prompt is recorded as the parent's, and a subagent's wave clears only at the
parent's next clearing event.
SubagentStop MUST be registered: it is a subagent's own turn-end, the
backstop that resolves that helper's share of the awaiting flag when no
per-tool clearing event does -- without it, that share stays until the
parent's SessionEnd, the watcher's self-heal, or the 2h stale sweep. Maintains
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
  - PermissionRequest -> NO wave. Append an attribution hint
    {agent_id|None, tool_use_id|None, ts} to <pending_dir>/<session_id>
    (see _PENDING_TTL_SEC): it also fires for calls auto mode resolves
    with no prompt ever shown.
  - Notification with notification_type == permission_prompt (a prompt is
    actually on screen) -> consume EVERY hint younger than 10s and add one
    owner line PER hint (its `agent:<id>`, or `parent` for an untagged one)
    to <awaiting_input_dir>/<session_id> (Claude is BLOCKED on you); with
    none live, add a `parent` owner line.
    idle_prompt is deliberately ignored -- see _HANDLED_NOTIFICATION_TYPES.
  - PreToolUse with tool_name in {AskUserQuestion, ExitPlanMode}
    -> write <awaiting_input_dir>/<session_id>  (blocked on a human
    answer/approval that fires NO permission_prompt -- see
    _AWAITING_INPUT_TOOLS). The PostToolUse when you answer clears it.
  - UserPromptSubmit / PostToolUse / PostToolUseFailure / PermissionDenied /
    Stop / StopFailure -> resolve the caller's share of
    <awaiting_input_dir>/<session_id> (you replied / a tool ran or failed /
    the request was auto-denied / the turn ended), and drop the caller's
    spent hint(s) (PermissionDenied: logged PERMDENIED_DROP)
  - SessionEnd -> remove <awaiting_input_dir>/<session_id> outright (every
    owner's share), <pending_dir>/<session_id>, and <recap_dir>/<session_id>
    (session is gone)
  - SubagentStop (a helper's own turn end) -> resolve that helper's share
    of <awaiting_input_dir>/<session_id> and drop its hints; nothing else.

  The awaiting flag is an OWNER SET (see _OWNER_PARENT): line 1 is the
  type of the latest raise, then one line per pending prompt owner with its
  add epoch -- `parent <epoch>` for the main thread, `agent:<agent_id>
  <epoch>` for a subagent (a helper shares its parent's session_id). Every
  write ADDS the writer's own line; every clearing event removes only the
  caller's own line; owners older than 2h are pruned on every update; the
  file's mtime is the newest owner's add time; the file goes when its last
  owner resolves. All of it runs under an fcntl.flock.
  Every other helper (agent_id) event touches nothing -- see the helper
  gate in main().

  Accepted limitation: agent_id identifies the AGENT, not the prompt. A
  helper running tool calls in parallel can clear its own wave when a
  sibling tool call finishes while another of its calls is still waiting
  on the user. The parent path has always behaved the same way (any parent
  PostToolUse clears the parent's share).
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
another dedicated diagnostic pass. Verified live (2.1.282): a subagent's
events -- SubagentStop included -- carry agent_id; they are routed through
the helper gate in main(), which never lets them touch the parent's
finished / failed / recapping / turn flags.

Established (round 4) -- and only this:
  - Documented semantics (code.claude.com/docs/en/hooks): PermissionRequest
    fires when a tool call needs a permission decision, INCLUDING calls auto
    mode's classifier then resolves itself; PermissionDenied fires when auto
    mode denies a call (what a MANUAL interactive denial fires is not
    documented); Notification permission_prompt fires only when a prompt UI
    is actually shown; PostToolUse fires on success only, PostToolUseFailure
    after a tool call fails. Hence Notification raises the wave and
    PermissionRequest only attributes it.
  - Observed (live claude_hook.log + a headless `claude -p` 2.1.283 probe
    logging payload KEYS only): every real Notification permission_prompt
    is UNTAGGED -- no agent_id -- including ones raised while subagents ran;
    a subagent's PermissionRequest carries agent_id + agent_type, a
    main-thread one carries no agent_id; SubagentStop carries agent_id.
  - Accepted limitations (see _PENDING_TTL_SEC): when several hints are
    live for one session the Notification cannot tell whose prompt it is,
    so every live hint's owner co-owns it and the wave can outlast the real
    prompt until the others resolve too (it never guesses one); a prompt
    that fires NO PermissionRequest of its own while exactly one unrelated
    hint is live is attributed to that hint's owner; and a manually denied
    prompt that fires no further per-tool event clears only at its turn's
    Stop / SubagentStop.
PreCompact/PostCompact are documented (see Claude Code hooks-guide.md) but
not yet independently verified against a live payload either -- same
log-based confirmation applies after a real /compact.

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

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from collections.abc import Iterator

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
# Round 4: PermissionRequest's attribution hints -- see _PENDING_TTL_SEC.
# Deliberately NOT inside FLAG_DIR: nothing in here is a wave.
PENDING_DIR = os.path.join(_SQUID_PET_HOME, "claude_permission_pending")
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
#
# Pink-2026-09-24: StopFailure added. It is the errored-turn twin of Stop --
# the turn ended, so whatever it was waiting on is moot. Without it, a turn
# that fails on an API error while a permission prompt is up left the
# awaiting-input flag set, so "your turn" kept waving until the 2h sweep and
# (approval taking prime over concerned) masked the concerned sprite entirely.
#
# Round 3: PermissionDenied added -- documented as auto mode denying a tool
# call. That request is over, so it clears the caller's share exactly like
# PostToolUse (a no-op safety net since round 4: an auto-denied call never
# raised a wave, but a future Claude Code could send both a Notification and
# a PermissionDenied for one prompt).
#
# Round 4: PostToolUseFailure added -- PostToolUse fires only on SUCCESS, so
# an approved tool that then FAILS resolved its prompt just the same and must
# clear exactly like PostToolUse (helpers included, via the helper gate).
_REMOVE_ON_EVENTS = frozenset({
    "UserPromptSubmit", "SessionEnd", "PostToolUse", "PostToolUseFailure",
    "Stop", "StopFailure", "PermissionDenied",
})

# Pink-2026-09-27: helper (agent_id) events that resolve that helper's OWN
# share of the awaiting flag -- exactly the parent's clearing events, plus
# SubagentStop (the helper's own Stop: the backstop when no per-tool event
# resolves its prompt -- e.g. a manual denial, whose events are undocumented;
# it must be registered, see the module docstring). Never PreToolUse, which
# only ever writes the flag.
# See the helper gate in main().
_HELPER_OWN_CLEAR_EVENTS = _REMOVE_ON_EVENTS | {"SubagentStop"}

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


# Set when a tty lookup could not be carried out at all (the `ps` snapshot
# failed or timed out), as opposed to running fine and finding no terminal.
# The two look identical to callers of _controlling_tty() -- both give None --
# but they must NOT be treated the same: "no terminal" is a fact we can act
# on, "could not find out" is not. Module-level is safe here because the hook
# is a single-shot process: one event, one lookup, then exit.
_TTY_LOOKUP_FAILED = False


def _ancestor_tty_via_ps() -> str | None:
    """First ancestor process (starting from this hook) that has a real
    controlling terminal, via a single `ps` snapshot walked in memory.

    One subprocess call, not one per hop. Since Pink-2026-09-19 this runs once
    per turn (see _record_session_tty), not on every event. Any failure yields
    None and sets _TTY_LOOKUP_FAILED (best-effort)."""
    global _TTY_LOOKUP_FAILED
    import subprocess
    try:
        r = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,tty="],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        _TTY_LOOKUP_FAILED = True
        return None
    # Parse FIRST: `ps` exits nonzero merely because it could not read some
    # unrelated process, while still printing a perfectly usable table. Only
    # if the walk found nothing do we ask whether the run itself was sound --
    # a bad run means "could not find out", not "no ancestor has a terminal",
    # and the caller must keep any existing entry rather than delete it on the
    # strength of an unusable snapshot.
    me = os.getpid()
    tty = _first_ancestor_tty(r.stdout, me)
    if tty is None and (
        r.returncode != 0
        or not r.stdout.strip()
        # Our OWN pid is always running, so its absence means the snapshot is
        # filtered/partial (sandboxed or shimmed `ps`), not that the walk
        # legitimately reached the top without finding a terminal. Without
        # this the caller would delete a valid entry over an unusable table.
        or not _pid_in_snapshot(r.stdout, me)
    ):
        _TTY_LOOKUP_FAILED = True
    return tty


def _pid_in_snapshot(ps_output: str, pid: int) -> bool:
    """Whether `pid` appears as a pid (first column) in a ps snapshot."""
    want = str(pid)
    for line in ps_output.splitlines():
        parts = line.split()
        if parts and parts[0] == want:
            return True
    return False


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


def _touch_session_tty(session_id: str) -> None:
    """Bump an EXISTING tty entry's mtime so the watcher's 2h stale sweep does
    not evict a still-live session mid-long-turn. Cheap and total: no `ps`, no
    write, creates nothing (a missing entry raises FileNotFoundError, which is
    swallowed), and never raises -- hooks block the turn."""
    try:
        os.utime(os.path.join(TTY_DIR, session_id))
    except OSError:
        pass


def _record_session_tty(session_id: str, event: str) -> None:
    """Best-effort: remember which terminal this session lives in. Never fails
    the hook; a missing tty simply leaves no entry (the reader falls back to
    the cwd guess).

    Pink-2026-09-19 -- resolved ONLY on _TURN_OPEN_EVENTS. This used to run on
    every event. Claude Code spawns hooks with no controlling terminal
    (verified live: the hook process shows `tty ??`), so _own_controlling_tty()
    always fails and _ancestor_tty_via_ps() runs, forking `ps -Ao` over every
    process on the machine -- measured at 73-140ms. PostToolUse fires after
    every tool call and hooks BLOCK the turn, so a 30-tool turn was paying 2+
    seconds for a value that had not changed.

    Once per turn is both sufficient and necessary:
      - sufficient, because a session's tty is fixed for the life of its
        process, and the one way a session_id legitimately moves hosts
        (`claude --resume` in another tab) cannot happen without submitting a
        prompt first;
      - necessary, because anything cheaper would have to trust a cached
        value across a resume.

    Deliberately NOT "skip when an entry already exists": that variant still
    paid the full cost on every event for any session where no ancestor has a
    terminal at all (`claude -p`, CI, a detached spawn), since the lookup fails
    and no file is ever written -- precisely the population the change is
    supposed to relieve. The cost of this rule is that a hook installed
    mid-session has no entry until that session's next prompt; one turn of
    cwd-guess fallback is a fair price.
    """
    if event not in _TURN_OPEN_EVENTS:
        # Pink-2026-09-24: keep a live session's entry from ageing out of the
        # watcher's 2h stale sweep during a long turn with no new prompt.
        # Cheaply bump its mtime IF it already exists -- no `ps`, no write, no
        # new entry, and never raises (hooks block the turn, so this stays off
        # the expensive path that only turn-open pays).
        _touch_session_tty(session_id)
        return
    tty = _controlling_tty()
    path = os.path.join(TTY_DIR, session_id)
    if not tty:
        if _TTY_LOOKUP_FAILED:
            # We could not find out -- `ps` failed or timed out on a loaded
            # machine. Keep whatever we had: it is probably still correct, and
            # this is now the only refresh point, so destroying a valid entry
            # over a transient hiccup would strand the whole turn on the cwd
            # guess (wrong window whenever two sessions share a directory in
            # different hosts -- exactly what this registry prevents).
            return
        # Lookup ran and found no terminal: a fact. Drop any existing entry
        # rather than keep a value we just decided needed refreshing -- after
        # a `claude --resume` into a different tab, a stale tty sends "take me
        # there" to a confidently wrong window, while no entry merely falls
        # back to the cwd guess.
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    if not _ensure_dir(TTY_DIR, "tty"):
        return
    # tmp + rename (see _atomic_write) so a reader never sees a half-written
    # entry, and a failed write cannot truncate a good entry into a zero-byte
    # one; the helper also removes its tmp if the write fails. The pid in the
    # tmp name keeps that true even if two hooks for the same session ever
    # overlap (they should not now that this is turn-open only).
    try:
        _atomic_write(path, tty)
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


# Session ids AND agent ids become filenames / flag-file lines, so both must
# match this charset (Claude Code session ids are UUIDs, agent ids are hex --
# well inside it). Anything else is refused: it could raise (a non-string),
# escape a dir, or -- for an agent id containing a newline -- inject a forged
# owner line into the awaiting flag.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _atomic_write(path: str, content: str, mtime_ns: int | None = None) -> None:
    """tmp + os.replace, shared by every whole-file publish in this script
    (the awaiting-input flag, the tty registry): a reader -- or a sibling
    hook process -- never sees a half-written file, and a failed write can
    never truncate a good one into a zero-byte one.

    The tmp is a DOTFILE in the same dir as `path`: same filesystem so the
    rename is atomic, and dot-prefixed so the watcher's flag-dir scans (which
    list every NON-dot entry as a live session) never mistake it --
    transiently, or as a leftover after a kill mid-write -- for a phantom
    session id. The pid keeps concurrent writers off each other's tmp. On
    failure the tmp is removed and the error re-raised for the caller to log.

    `mtime_ns`, if given, is stamped on the TMP before the rename (review
    round 3 #5), so the published file never exists -- not even for an
    instant between a rename and a later utime -- with the wrong mtime. The
    watcher re-arms its alert when a flag's mtime advances, so a transient
    fresh mtime would read as a brand-new prompt.
    """
    tmp = os.path.join(
        os.path.dirname(path), f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as f:
            f.write(content)
        if mtime_ns is not None:
            os.utime(tmp, ns=(mtime_ns, mtime_ns))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Pink-2026-09-27 (review rounds 2 + 3): the awaiting-input flag is an OWNER
# SET.
#
#   line 1      the notification_type / tool name of the most recent raise
#               (informational -- watcher.py and focus.py never act on it)
#   line 2..n   one owner per still-pending prompt, with the epoch it was
#               added: `parent <epoch>` for a main-thread prompt,
#               `agent:<agent_id> <epoch>` for a subagent (Task-tool helper).
#               A legacy line with no epoch (older hook) takes the file mtime.
#
# A helper shares its parent's session_id, so several independent prompts --
# the parent's own, and any number of concurrently-running helpers' -- can be
# pending on the SAME file at once. Every WRITE adds only the writer's own
# owner line (never overwrites anyone else's); every CLEAR removes only the
# clearer's own line; the file is unlinked when the last owner resolves. That
# is the whole rule: the parent can no longer clear a helper's wave, a helper
# can never clear the parent's or a sibling's, and nobody's write erases
# anybody else's ownership. A legacy flag with no owner lines (written by an
# older hook) is read as `parent`-owned.
#
# mtime (round 3 #6): every add and every remove prunes owners older than
# _OWNER_MAX_AGE_SEC, and the file's mtime is the newest remaining owner's add
# time -- so an owner that died without reporting back ages out on its own
# even while siblings keep raising new prompts (before, each add refreshed the
# mtime and the watcher's 2h sweep never fired). An add sets it to now; a
# clear NEVER advances it (the watcher reads an advance as a new prompt and
# re-alerts).
#
# All read-modify-write-or-unlink of the file happens under an exclusive
# fcntl.flock on _FLAG_LOCK_NAME in the flag dir (same pattern as
# scripts/codex_pet_hook.py): hooks for one session -- a parent and its
# fanned-out helpers -- can run as simultaneous processes, and an unlocked
# read-modify-write loses or resurrects owners. REMOVALS take the lock too,
# even to find there is nothing to remove (Codex round 2 P1): a lock-free "no
# flag" check could overtake an add already in flight and leave its wave
# behind. The lock is held for a read and a rename, never across anything
# slow, and flock is released by the kernel if the holder dies.
_OWNER_PARENT = "parent"
_OWNER_AGENT_PREFIX = "agent:"
_FLAG_LOCK_NAME = ".lock"
# Matches watcher.CLAUDE_AWAITING_INPUT_STALE_SEC (the 2h crash-safety sweep).
_OWNER_MAX_AGE_SEC = 7200.0
_TYPE_PERMISSION_PROMPT = "permission_prompt"


class _FlagReadError(Exception):
    """The flag exists (or may) but could not be read. The caller must abort
    the whole flag operation -- no write, no unlink -- rather than guess at
    who owns it (round 3 #9: guessing `parent` let a later write erase every
    real owner, and a parent clear delete a helper's wave)."""

    def __init__(self, errno_name: str) -> None:
        super().__init__(errno_name)
        self.errno_name = errno_name


def _errno_name(e: OSError) -> str:
    import errno
    return errno.errorcode.get(e.errno or 0, str(e.errno))


def _agent_owner(agent_id: str) -> str:
    return f"{_OWNER_AGENT_PREFIX}{agent_id}"


def _is_owner_name(name: str) -> bool:
    if name == _OWNER_PARENT:
        return True
    return (name.startswith(_OWNER_AGENT_PREFIX) and bool(
        _SAFE_SESSION_ID.fullmatch(name[len(_OWNER_AGENT_PREFIX):])))


def _parse_owner_line(line: str, default_ts: float) -> tuple[str, float] | None:
    parts = line.split(" ")
    if not parts or len(parts) > 2 or not _is_owner_name(parts[0]):
        return None
    if len(parts) == 1:
        return parts[0], default_ts
    try:
        ts = float(parts[1])
    except ValueError:
        return None
    if ts != ts or ts in (float("inf"), float("-inf")):
        return None
    return parts[0], ts


@contextlib.contextmanager
def _flag_lock() -> Iterator[None]:
    """Exclusive lock over the awaiting-input flag dir (which must exist).
    Dot-prefixed so the watcher's scan and stale sweep never see it."""
    with open(os.path.join(FLAG_DIR, _FLAG_LOCK_NAME), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _read_owners(flag_path: str) -> tuple[str, dict[str, float], int] | None:
    """(type line, {owner: add epoch}, file mtime_ns) of the flag, or None if
    it does not exist. Call under _flag_lock. A flag with no recognisable
    owner lines is legacy and `parent`-owned (added at the file mtime). Any
    OTHER read error raises _FlagReadError -- never a fabricated owner."""
    try:
        with open(flag_path, errors="replace") as f:
            mtime_ns = os.fstat(f.fileno()).st_mtime_ns
            lines = f.read().splitlines()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise _FlagReadError(_errno_name(e)) from e
    file_ts = mtime_ns / 1e9
    owners: dict[str, float] = {}
    for line in lines[1:]:
        parsed = _parse_owner_line(line, file_ts)
        if parsed is not None:
            owner, ts = parsed
            owners[owner] = max(ts, owners.get(owner, ts))
    if not owners:
        owners[_OWNER_PARENT] = file_ts
    return (lines[0] if lines else ""), owners, mtime_ns


def _prune_owners(owners: dict[str, float], now: float) -> dict[str, float]:
    """Drop owners added more than _OWNER_MAX_AGE_SEC ago; clamp a future add
    time (clock skew) to now so it can still age out."""
    return {o: min(ts, now) for o, ts in owners.items()
            if now - ts <= _OWNER_MAX_AGE_SEC}


def _render_flag(ntype: str, owners: dict[str, float]) -> str:
    return "\n".join([ntype, *(f"{o} {owners[o]:.6f}" for o in sorted(owners))])


def _add_flag_owner(session_id: str, ntype: str, owner: str) -> None:
    """Raise (or re-raise) the wave for `owner` at the current time, keeping
    every other still-live owner already recorded. The type line becomes this
    raise's type and the mtime becomes now -- a new raise IS fresh. FLAG_DIR
    must exist. Raises _FlagReadError / OSError on failure -- the caller
    logs it."""
    with _flag_lock():
        _add_flag_owner_locked(session_id, ntype, owner)


def _add_flag_owner_locked(session_id: str, ntype: str, owner: str) -> None:
    """_add_flag_owner's body, for a caller that already holds _flag_lock
    (flock is per open file description: re-taking it from a second open in
    the same process would deadlock)."""
    _add_flag_owners_locked(session_id, ntype, [owner])


def _add_flag_owners_locked(session_id: str, ntype: str,
                            new_owners: list[str]) -> None:
    """Add every one of `new_owners` (at least one) at the current time in ONE
    write, so the watcher never sees a partial owner set. Call under
    _flag_lock."""
    flag_path = os.path.join(FLAG_DIR, session_id)
    now_ns = time.time_ns()
    now = now_ns / 1e9
    current = _read_owners(flag_path)
    owners = _prune_owners(current[1], now) if current is not None else {}
    for owner in new_owners:
        owners[owner] = now
    _atomic_write(flag_path, _render_flag(ntype, owners), mtime_ns=now_ns)


def _remove_flag_owner(session_id: str, owner: str) -> str:
    """Resolve `owner`'s own share of the flag, pruning expired owners on the
    way. FLAG_DIR must exist. Returns one of:
      "NOFLAG"   no flag at all
      "NOTOWNER" `owner` holds no (live) share -- only expired owners, if
                 any, were pruned
      "REMOVED"  no owner is left -- the flag is gone
      "KEPT"     `owner`'s line removed; other owners are still pending, so
                 the wave stays. The mtime becomes the newest remaining
                 owner's add time but never ADVANCES: a partial resolve must
                 not make an older wave look freshly raised (the watcher
                 re-alerts on an advance; focus.py picks the freshest wave;
                 self-heal and the 2h sweep age it).
    Raises _FlagReadError / OSError on failure -- the caller logs it."""
    flag_path = os.path.join(FLAG_DIR, session_id)
    now = time.time()
    # No lock-free "no flag" fast path (Codex round 2 P1): see _OWNER_PARENT.
    with _flag_lock():
        current = _read_owners(flag_path)
        if current is None:
            return "NOFLAG"
        ntype, raw, mtime_ns = current
        owners = _prune_owners(raw, now)
        was_owner = owner in owners
        owners.pop(owner, None)
        if not was_owner and set(owners) == set(raw):
            return "NOTOWNER"
        if len(owners) < len(raw) - (1 if was_owner else 0):
            _log(f"{session_id} PRUNED {len(raw) - len(owners) - was_owner} "
                 "expired owner(s)")
        if not owners:
            os.unlink(flag_path)
            return "REMOVED" if was_owner else "NOTOWNER"
        # Owner times are rendered to the microsecond, so a newest remaining
        # owner within 1us of the file mtime IS the raise that set it: keep
        # the exact mtime_ns rather than a float-rounded copy of it.
        newest_ns = int(round(max(owners.values()) * 1e9))
        if newest_ns >= mtime_ns - 1000:
            newest_ns = mtime_ns
        _atomic_write(flag_path, _render_flag(ntype, owners), mtime_ns=newest_ns)
        return "KEPT" if was_owner else "NOTOWNER"


def _remove_flag_entirely(session_id: str) -> bool:
    """SessionEnd only: the session -- helpers included -- is gone, so no
    owner can ever report back. Remove the flag outright (True) rather than
    leave helper shares for the 2h sweep; False if there was none. Takes the
    lock first, like every removal. FLAG_DIR must exist. Raises OSError on
    failure."""
    flag_path = os.path.join(FLAG_DIR, session_id)
    with _flag_lock():
        try:
            os.unlink(flag_path)
        except FileNotFoundError:
            return False
        return True


# Round 4: the PENDING-CORRELATION store -- PermissionRequest is
# attribution-only.
#
# PermissionRequest fires whenever a tool call needs a permission DECISION,
# including the ones auto mode's classifier resolves on its own with no human
# ever shown anything (code.claude.com/docs/en/hooks). Raising a wave on it
# (round 3) waved -- and, through the watcher's mtime re-arm, re-fired the OS
# banner -- for prompts nobody would ever see. Only Notification
# permission_prompt means a prompt UI is actually on screen, so it alone
# raises the wave. But a real Notification is UNTAGGED (no agent_id, even for
# a subagent's prompt), while PermissionRequest does carry agent_id for a
# subagent. So:
#
#   PermissionRequest  appends {agent_id|None, tool_use_id|None, ts} to
#                      <PENDING_DIR>/<session_id> -- a hint, never a wave.
#   Notification       consumes EVERY live entry for the session (younger
#                      than _PENDING_TTL_SEC) and adds one owner PER entry
#                      (`agent:<id>`, or `parent` for an untagged one) to
#                      the awaiting flag, in one write (round 6 -- see
#                      below). No live entry -> `parent`, as before.
#   PermissionDenied   (auto-mode denial -- no Notification ever follows)
#                      pops the caller's oldest entry (or, if the caller has
#                      none, the session's oldest).
#   PostToolUse / PostToolUseFailure  pop the caller's OWN oldest entry: an
#                      auto-approved call's tool ran, so its hint is spent and
#                      must not be paired with someone else's later prompt.
#   Stop / StopFailure / UserPromptSubmit / SubagentStop  drop ALL the
#                      caller's entries (its turn is over).
#   SessionEnd         removes the session's file outright -- under the
#                      lock too (round 5), so a PermissionRequest mid-publish
#                      cannot land an orphan file just after it.
#
# The store lives in its OWN directory, never the awaiting-input flag dir, so
# the watcher can never mistake a hint for a wave. Entries are capped at
# _PENDING_MAX_ENTRIES per session (oldest dropped), expired ones are pruned
# on every write, and the watcher sweeps any file untouched for 5 minutes
# (claude/crash with no SessionEnd). Every read-modify-write runs under the
# same _flag_lock as the owner set, so a Notification's pop and its owner add
# are one atomic step.
#
# NO GUESSING -- every live entry becomes an owner (round 6). An
# auto-approved call's hint stays live until its tool's PostToolUse; if a
# DIFFERENT caller's real prompt comes up meanwhile, that prompt's own
# PermissionRequest makes two live entries and the untagged Notification
# cannot tell which is on screen.
#   - Round 4 paired FIFO and could hand the prompt to the still-running
#     helper: `agent:<helper>` then owned someone else's wave, and the
#     helper's delayed PostToolUse wiped it (Codex round 4 P1).
#   - Round 5 gave two or more live entries to bare `parent` -- but when the
#     real prompt was a helper's, the PARENT's own next unrelated
#     PostToolUse / Stop / UserPromptSubmit wiped it while the helper's
#     prompt was still unanswered (Codex round 5 P1): the same failure shape,
#     a wave owned by the wrong scope and cleared by that scope.
#   - Round 6: each live entry adds its OWN owner line, consuming all of
#     them, whatever their count (one is just the degenerate case). A wave
#     clears only when EVERY owner line is gone (the owner set above), so
#     each candidate's share stays until that candidate's own resolution:
#     {parent, helperX} -> the parent's unrelated event removes only
#     `parent`; `agent:helperX` holds the wave until helperX resolves. The
#     cost is the safe direction for "you need to approve something": with
#     several candidates the wave can outlast the real prompt until the
#     others resolve too. Only entries actually live at Notification time
#     become owners -- no extra `parent` is ever synthesised alongside them:
#     that would make even a cleanly single-attributed helper prompt wait for
#     the parent's whole turn, defeating a helper's prompt clearing on the
#     helper's own resolution.
#
# Accepted limitations (documented, pinned by tests, not bugs to chase):
#   - Over-attribution. With several live entries, an entry whose call was
#     auto-approved and is still running co-owns a prompt that is not its
#     own; its share clears at its own PostToolUse / Stop / SubagentStop.
#     Same category as "agent_id identifies the agent, not the prompt" above.
#   - Misattribution of a prompt with no PermissionRequest of its own. Some
#     prompts fire no PermissionRequest at all (documented: sandbox network
#     prompts; possibly an auto-mode classifier edge case), and the hook may
#     be unregistered. If exactly one UNRELATED hint is live at that
#     Notification, it is consumed and the prompt is attributed to its
#     owner -- whose own resolution may then clear a wave that is still up.
#     The TTL (see _PENDING_TTL_SEC) keeps that window small. Closing it entirely would mean
#     never trusting even a single live hint (e.g. always adding `parent`
#     too), which gives up the core per-helper attribution above for a rare
#     edge case -- so it is kept deliberately. Review record: reviewers
#     disagreed on its severity -- one assessed it as an accepted,
#     pre-existing, disclosed, non-regressing limitation; another, independent
#     reviewer rated it P1. The tradeoff above is why it stays.
#   - A MANUALLY denied interactive prompt's event behaviour is undocumented.
#     If no PostToolUse / PostToolUseFailure / PermissionDenied fires for it,
#     the wave Notification raised clears only at the turn's own Stop /
#     SubagentStop -- bounded by the turn, no worse than before round 3.
# Round 5: shrunk 30s -> 5s (defence in depth for the limitations above). A
# real prompt's Notification follows its own PermissionRequest almost at once
# -- putting the prompt UI on screen does not wait on the human -- so a few
# seconds is generous for the legitimate pairing.
# Round 6: an independent reviewer twice cited a "documented ~6s delay before
# permission_prompt Notifications fire" as a reason 5s was too tight (a
# helper's hint could expire before its own Notification lands, falling back
# to a bare `parent` owner instead of `agent:<id>`). Checked directly against
# code.claude.com/docs/en/hooks on 2026-09-28, including a full scan for every
# numeric time value on that page: no such delay is documented anywhere, so
# the specific figure is unverified and may be a fabricated citation. That
# said, round 6 made TTL length far less safety-critical: an ambiguous or
# expired hint now either becomes a safe co-owner or falls back to `parent`
# (never a wrong guess), so widening the TTL costs little even if the citation
# turns out to be right about *something* Codex has non-public visibility into
# (e.g. an implementation detail not yet in the public docs). Raised to 10s as
# cheap insurance either way -- not a concession that the 6s claim was
# verified, just a low-cost hedge now that being wrong about it is harmless.
_PENDING_TTL_SEC = 10.0
_PENDING_MAX_ENTRIES = 8
# Caller-scoped pending drops (see above): one entry vs every entry.
_PENDING_DROP_ONE_EVENTS = frozenset({
    "PostToolUse", "PostToolUseFailure", "PermissionDenied"})
_PENDING_DROP_ALL_EVENTS = frozenset({
    "Stop", "StopFailure", "UserPromptSubmit", "SubagentStop"})


def _safe_id_or_none(value: object) -> str | None:
    return (value if isinstance(value, str)
            and _SAFE_SESSION_ID.fullmatch(value) else None)


def _read_pending(path: str) -> list[dict]:
    """The session's pending entries, oldest first. Missing file -> [].
    Malformed lines are skipped. Call under _flag_lock. Raises OSError on a
    real read failure (the caller logs it and treats the hint as absent)."""
    try:
        with open(path, errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    entries: list[dict] = []
    for line in lines:
        try:
            obj = json.loads(line)
            ts = float(obj["ts"])
        except (ValueError, TypeError, KeyError):
            continue
        if not isinstance(obj, dict) or ts != ts:
            continue
        entries.append({"agent_id": _safe_id_or_none(obj.get("agent_id")),
                        "tool_use_id": _safe_id_or_none(obj.get("tool_use_id")),
                        "ts": ts})
    return entries


def _write_pending(path: str, entries: list[dict]) -> None:
    """Publish `entries` (or remove the file when there are none). Call
    under _flag_lock."""
    if not entries:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    _atomic_write(path, "".join(json.dumps(e, sort_keys=True) + "\n"
                                for e in entries))


def _live_pending(entries: list[dict], now: float) -> list[dict]:
    """Entries still within the TTL (a future ts -- clock skew -- counts as
    live only if within the TTL of now too)."""
    return [e for e in entries if abs(now - e["ts"]) <= _PENDING_TTL_SEC]


def _record_pending(session_id: str, agent_id: str | None,
                    tool_use_id: str | None) -> int:
    """PermissionRequest: append this request's hint. Returns the number of
    live entries now pending. PENDING_DIR and FLAG_DIR must exist. Raises
    OSError on failure."""
    path = os.path.join(PENDING_DIR, session_id)
    now = time.time()
    with _flag_lock():
        entries = _live_pending(_read_pending(path), now)
        entries.append({"agent_id": agent_id, "tool_use_id": tool_use_id,
                        "ts": now})
        entries = entries[-_PENDING_MAX_ENTRIES:]
        _write_pending(path, entries)
        return len(entries)


def _pop_pending_locked(session_id: str, *, agent_id: str | None,
                        fallback_any: bool = False) -> dict | None:
    """Remove and return the CALLER's oldest live entry (agent_id None = the
    parent); with fallback_any, the session's oldest if the caller has none.
    Expired entries are pruned on the way. Call under _flag_lock. (An
    untagged Notification uses _take_all_pending_locked instead.)"""
    path = os.path.join(PENDING_DIR, session_id)
    raw = _read_pending(path)
    if not raw:
        return None
    entries = _live_pending(raw, time.time())
    idx = next((i for i, e in enumerate(entries)
                if e["agent_id"] == agent_id), None)
    if idx is None and fallback_any and entries:
        idx = 0
    taken = entries.pop(idx) if idx is not None else None
    if taken is not None or len(entries) != len(raw):
        _write_pending(path, entries)
    return taken


def _take_all_pending_locked(session_id: str) -> list[dict]:
    """Untagged Notification: remove and return EVERY live entry pending for
    the session, oldest first ([] with none); expired ones are pruned with
    them, so the store is left empty. The caller adds one owner per entry
    (round 6; see the comment above _PENDING_TTL_SEC) -- no count is
    special-cased. Call under _flag_lock. Raises OSError on a real read
    failure."""
    path = os.path.join(PENDING_DIR, session_id)
    raw = _read_pending(path)
    if not raw:
        return []
    _write_pending(path, [])
    return _live_pending(raw, time.time())


def _hint_owner(hint: dict) -> str:
    """The owner a pending hint stands for: its helper, else the parent."""
    return _agent_owner(hint["agent_id"]) if hint["agent_id"] else _OWNER_PARENT


def _remove_pending_store(session_id: str) -> None:
    """SessionEnd: remove the session's pending store outright. Under
    _flag_lock like every other pending-store mutation, so it cannot race a
    PermissionRequest's publish (round 5, Codex round 4 P2). FLAG_DIR must
    exist. Raises OSError on failure (a missing store is not one)."""
    with _flag_lock():
        _write_pending(os.path.join(PENDING_DIR, session_id), [])


def _drop_caller_pending(session_id: str, event: str,
                         agent_id: str | None) -> int:
    """A clearing event: drop the caller's own pending hint(s) -- one for
    _PENDING_DROP_ONE_EVENTS, all for _PENDING_DROP_ALL_EVENTS. Returns how
    many were dropped. A no-op (no dir created) when there is no store.
    Raises OSError on failure."""
    if not os.path.exists(os.path.join(PENDING_DIR, session_id)):
        # Lock-free fast path is safe HERE (unlike the flag's removal): the
        # hint for the call this event resolves was written before the event
        # could fire (a hook blocks its caller), and an append still in
        # flight belongs to a different, still-pending call that this event
        # must not drop anyway. Keeps the common PostToolUse path to one stat.
        return 0
    if not _ensure_dir(FLAG_DIR, event):
        return 0
    with _flag_lock():
        if event in _PENDING_DROP_ALL_EVENTS:
            path = os.path.join(PENDING_DIR, session_id)
            raw = _read_pending(path)
            kept = [e for e in _live_pending(raw, time.time())
                    if e["agent_id"] != agent_id]
            if len(kept) != len(raw):
                _write_pending(path, kept)
            return len(raw) - len(kept)
        taken = _pop_pending_locked(
            session_id, agent_id=agent_id,
            fallback_any=(event == "PermissionDenied"))
        return 0 if taken is None else 1

def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        _log(f"PARSE_ERROR {e!r}")
        return 0

    if not isinstance(payload, dict):
        _log("PARSE_ERROR payload is not an object")
        return 0
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")
    if not session_id:
        _log(f"{event} NO_SESSION_ID")
        return 0
    # session_id becomes a filename in several flag dirs, and event is used in
    # set lookups; refuse anything that could raise or escape a dir. Claude
    # Code session ids are UUIDs, well inside this charset.
    if not isinstance(event, str):
        _log("PARSE_ERROR hook_event_name is not a string")
        return 0
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
        _log(f"{event} BAD_SESSION_ID")
        return 0

    # Pink-2026-09-24 (review findings 2 + round-2 A): a subagent (Task-tool
    # helper) event carries the PARENT session_id PLUS an agent_id (main-thread
    # events NEVER carry agent_id). A helper shares the parent's session_id, so
    # letting it write/clear the parent's per-session flags corrupts the parent's
    # state -- e.g. an ExitPlanMode raising a wave that later never clears, a
    # PostToolUse clearing a wave the parent genuinely raised, or (round-2) a
    # Stop/StopFailure/UserPromptSubmit/PreCompact/PostCompact/SessionEnd writing
    # claude_finished / claude_failed, opening or closing the turn bracket, or
    # touching recapping under the parent's id. All three dispatch chains below
    # are therefore gated behind ONE early return (see below) rather than a
    # scatter of `not is_helper` checks.
    #
    # Verified live (Claude Code 2.1.282/2.1.283): subagent PreToolUse,
    # PostToolUse, PermissionRequest, SubagentStart and SubagentStop all carry
    # agent_id (+ agent_type); main-thread events carry none, and a real
    # Notification never does (see the module docstring). PermissionRequest
    # and Notification are DELIBERATELY the exceptions: they run before the
    # gate so a permission prompt (even a background helper's, which Claude
    # Code surfaces in the main session) still waves -- a genuine "blocked on
    # the USER" moment. The early return also
    # means SubagentStop (which must be registered -- see the docstring) and
    # any SubagentStart, both carrying agent_id, are handled by the gate and
    # never mislabelled by the trailing UNKNOWN_EVENT guard.
    raw_agent_id = payload.get("agent_id")
    # Round 3 (review #7): PRESENCE decides, not truthiness -- an agent_id of
    # "", [], {} or 0 is a helper event with an unusable id (BAD_AGENT_ID
    # below), never a main-thread one that may clear the parent's share.
    is_helper = "agent_id" in payload and raw_agent_id is not None
    # Pink-2026-09-27 (review round 2, Claude #4): agent_id goes into the
    # awaiting flag as an owner line, so it must be a plain id -- a list
    # crashed the clear path (unhashable), and a newline would forge a
    # second owner line. Anything else is a helper with NO usable id: its
    # prompt is owned parent-style (so it still waves, and the parent's next
    # clearing event resolves it) and it can clear nothing. Logged without
    # the value.
    agent_id = (raw_agent_id if isinstance(raw_agent_id, str)
                and _SAFE_SESSION_ID.fullmatch(raw_agent_id) else None)
    if is_helper and agent_id is None:
        _log(f"{event} {session_id} BAD_AGENT_ID")

    # Record this session's terminal before dispatching, so every signal a
    # session can raise ("take me there") is resolvable to the exact tab/app
    # it fired from, not guessed by cwd. Cleared on SessionEnd below.
    # Kept for helper events too: it never *writes* a new entry off a helper
    # event (helpers don't fire turn-open), only bumps an existing one's mtime
    # so the parent session doesn't age out of the tty registry mid-helper-run.
    _record_session_tty(session_id, event)

    if event == "PermissionRequest":
        # Round 4: ATTRIBUTION ONLY -- never a wave. PermissionRequest also
        # fires for calls auto mode resolves with no prompt shown, so it
        # records a pending hint (see _PENDING_TTL_SEC) that the Notification
        # for a REAL prompt consumes. The awaiting-input flag is not touched.
        # Content-blind: tool_name / tool_input / permission_suggestions are
        # never read. Returns before the helper gate (a helper's request must
        # still be recordable).
        if not (_ensure_dir(FLAG_DIR, event) and _ensure_dir(PENDING_DIR, event)):
            return 0
        tag = " (helper)" if is_helper else ""
        try:
            n = _record_pending(session_id, agent_id,
                                _safe_id_or_none(payload.get("tool_use_id")))
            _log(f"PermissionRequest {session_id} PENDING{tag} n={n}")
        except Exception as e:
            _log(f"PermissionRequest {session_id} PENDING_FAILED {e!r}")
        return 0

    if event == "Notification":
        ntype = payload.get("notification_type", "")
        if ntype in _HANDLED_NOTIFICATION_TYPES:
            if not _ensure_dir(FLAG_DIR, event):
                return 0
            # A prompt is actually on screen: ADD its owner to the flag's
            # owner set (see _OWNER_PARENT) -- nobody else's share is touched.
            # Whose prompt: a tagged payload says so itself (only synthetic
            # ones ever are) and consumes that agent's own hint. Otherwise
            # (round 6) EVERY live PermissionRequest hint for the session is
            # consumed and adds its own owner -- `agent:<id>` or `parent` --
            # so no count is special-cased and nothing is guessed: each owner
            # clears only via its own caller's resolution (see
            # _PENDING_TTL_SEC). With none live, `parent` (the pre-round-3
            # behaviour: expired, never sent, or PermissionRequest not
            # registered). The take and the add share one lock hold, and all
            # owners land in one write.
            try:
                with _flag_lock():
                    hints: list[dict] = []
                    try:
                        if agent_id:
                            _pop_pending_locked(session_id, agent_id=agent_id)
                        else:
                            hints = _take_all_pending_locked(session_id)
                    except OSError as e:
                        _log(f"Notification {session_id} PENDING_READ_FAILED "
                             f"{_errno_name(e)}")
                        hints = []
                    if agent_id:
                        owners, src = [_agent_owner(agent_id)], "tag"
                    elif hints:
                        # dict.fromkeys: one line per distinct owner, in order.
                        owners = list(dict.fromkeys(_hint_owner(h) for h in hints))
                        src = "pending" + (f" n={len(hints)}" if len(hints) > 1 else "")
                    else:
                        owners, src = [_OWNER_PARENT], "fallback"
                    _add_flag_owners_locked(session_id, ntype, owners)
                kinds = {"parent" if o == _OWNER_PARENT else "helper" for o in owners}
                who = "+".join(k for k in ("parent", "helper") if k in kinds)
                _log(f"Notification {session_id} WRITE {ntype} ({who} via {src})")
            except _FlagReadError as e:
                _log(f"Notification {session_id} READ_FAILED {e.errno_name}")
            except Exception as e:
                _log(f"Notification {session_id} WRITE_FAILED {e!r}")
        else:
            _log(f"Notification {session_id} IGNORED {ntype!r}")
        # Notification touches no later chain; return so the helper gate below
        # never sees it (a helper's permission_prompt must still wave).
        return 0

    if (event in _PENDING_DROP_ONE_EVENTS or event in _PENDING_DROP_ALL_EVENTS) \
            and not (is_helper and agent_id is None):
        # Round 4: the caller's own PermissionRequest hint(s) are spent -- see
        # _PENDING_TTL_SEC. Runs for the parent and for helpers alike, before
        # the helper gate; a helper with no usable id can drop nothing.
        try:
            dropped = _drop_caller_pending(session_id, event, agent_id)
        except Exception as e:
            dropped = 0
            _log(f"{event} {session_id} PENDING_DROP_FAILED {e!r}")
        if event == "PermissionDenied":
            _log(f"PermissionDenied {session_id} PERMDENIED_DROP n={dropped}")
        elif dropped:
            _log(f"{event} {session_id} PENDING_DROP n={dropped}")

    if is_helper:
        # ONE gate for every dispatch chain below: a helper shares the parent's
        # session_id, so it must not write or clear any of the parent's flags
        # (awaiting / finished / failed / recapping / turn_active). Notification
        # is the sole exception and already returned above. Content-blind.
        #
        # Pink-2026-09-27: 3193981 correctly stopped a helper's events from
        # clearing the PARENT's pending approval, but left no path for a
        # helper's OWN approval to clear -- confirmed live: the wave (and the
        # approval_needed override, which takes prime over the whole cascade)
        # stuck for ~100s, until the PARENT's next UserPromptSubmit/Stop
        # cleared it, not the moment the helper's own prompt actually
        # resolved. Claude Code hooks block the firing session's own
        # execution, so once a helper fires a further CLEARING event its own
        # prompt has necessarily been resolved (approved -- that tool's
        # PostToolUse / PostToolUseFailure -- or denied -- PermissionDenied,
        # the helper's NEXT PostToolUse, or SubagentStop, the helper's own
        # turn-ended analogue of the parent's Stop). Only _HELPER_OWN_CLEAR_EVENTS qualify: the helper's
        # PreToolUse (blocked on the user again) never clears. The clear
        # removes ONLY this helper's own `agent:<agent_id>` line -- the
        # parent's share and any sibling helper's survive -- and only the
        # awaiting-input flag moves; no other chain below is reached.
        #
        # Logged as EITHER a clear or a skip, never both for the same event --
        # a stuck-wave debugging session grepping this log for one session_id
        # must not have to reconcile two contradictory lines for one event.
        result = "SKIP"
        if event in _HELPER_OWN_CLEAR_EVENTS and agent_id:
            # The removal always takes the lock, which lives in FLAG_DIR.
            if not _ensure_dir(FLAG_DIR, event):
                return 0
            try:
                result = _remove_flag_owner(session_id, _agent_owner(agent_id))
            except _FlagReadError as e:
                _log(f"{event} {session_id} READ_FAILED {e.errno_name}")
                return 0
            except Exception as e:
                _log(f"{event} {session_id} HELPER_CLEAR_FAILED {e!r}")
                return 0
        if result in ("REMOVED", "KEPT"):
            _log(f"{event} {session_id} HELPER_CLEAR_OWN (agent_id) {result}")
        else:
            _log(f"{event} {session_id} HELPER_SKIP (agent_id)")
        return 0

    if event == "PreToolUse":
        tool = payload.get("tool_name", "")
        if tool in _AWAITING_INPUT_TOOLS:
            if not _ensure_dir(FLAG_DIR, event):
                return 0
            try:
                _add_flag_owner(session_id, tool, _OWNER_PARENT)
                _log(f"PreToolUse {session_id} WRITE {tool}")
            except _FlagReadError as e:
                _log(f"PreToolUse {session_id} READ_FAILED {e.errno_name}")
            except Exception as e:
                _log(f"PreToolUse {session_id} WRITE_FAILED {e!r}")
        else:
            _log(f"PreToolUse {session_id} NOOP {tool!r}")
    elif event in _REMOVE_ON_EVENTS:
        # Pink-2026-09-24: no early `return 0` on a mkdir failure -- a turn-close
        # event (Stop/StopFailure/SessionEnd) MUST still reach the turn-close
        # block below, or turn_in_flight sticks and the stall path paints
        # "thinking" over a finished/errored turn.
        #
        # Pink-2026-09-27: the parent removes only its OWN share (`parent`);
        # a helper still blocked on its own prompt keeps waving until its own
        # clearing event. SessionEnd is the exception: the whole session,
        # helpers included, is gone, so nobody is left to resolve anything.
        if _ensure_dir(FLAG_DIR, event):
            try:
                if event == "SessionEnd":
                    result = "REMOVED" if _remove_flag_entirely(session_id) else "NOFLAG"
                else:
                    result = _remove_flag_owner(session_id, _OWNER_PARENT)
                if result == "REMOVED":
                    _log(f"{event} {session_id} REMOVED")
                elif result == "KEPT":
                    _log(f"{event} {session_id} KEPT (helper owner pending)")
                elif result == "NOTOWNER":
                    _log(f"{event} {session_id} NOOP (helper-owned flag)")
                else:
                    _log(f"{event} {session_id} NOOP (no flag)")
            except _FlagReadError as e:
                # Abort only the flag operation: a turn-close event must still
                # reach the turn-close block below.
                _log(f"{event} {session_id} READ_FAILED {e.errno_name}")
            except Exception as e:
                _log(f"{event} {session_id} REMOVE_FAILED {e!r}")
        if event == "SessionEnd" and os.path.isdir(FLAG_DIR):
            # Round 5 (Codex round 4 P2): under the shared lock, never a bare
            # unlink -- a PermissionRequest mid-publish must finish (and be
            # removed here) rather than land an orphan just after. The lock
            # lives in FLAG_DIR, ensured just above; if that failed, no
            # PermissionRequest could have recorded anything either (it
            # ensures FLAG_DIR first), and the watcher's sweep is the
            # backstop. A failure here never skips the cleanup below.
            try:
                existed = os.path.exists(os.path.join(PENDING_DIR, session_id))
                _remove_pending_store(session_id)
                if existed:
                    _log(f"{event} {session_id} PENDING_REMOVED")
            except Exception as e:
                _log(f"{event} {session_id} PENDING_REMOVE_FAILED {e!r}")
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
        #
        # Pink-2026-09-24: no early `return 0` if FAILED_DIR cannot be made --
        # the turn still ended, so the turn-close block below MUST run
        # regardless, or turn_in_flight sticks and the stall path keeps
        # painting "thinking" over the error.
        if _ensure_dir(FAILED_DIR, event):
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
