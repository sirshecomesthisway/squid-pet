"""focus.py -- "take me to the window that's waiting".

Pink-2026-09-01: double-clicking a waving Squid should land you in the
session that actually needs you, not merely acknowledge it.

Matching is by TTY, which is exact. A Claude Code process has a
controlling terminal (`/dev/ttysNNN`); Terminal.app exposes the same
value as `tty of tab`. Comparing those two identifies the precise tab,
with nothing inferred.

The first attempt matched the terminal TITLE against the session's
working directory, which meant recording cwd on the awaiting-input flag
and widening what docs/PRIVACY.md promises. It was then measured against
a live window and did not even work: Claude Code sets the title to a
summary of the current task ("pinksmac -- Squid demo script with status
mentions"), not the directory. TTY needs no new stored data at all, so
the flag's contents are unchanged.

Two levels, because only one is universally available:

  1. THE APP. watcher.find_terminal_app_bundle_for_claude_code() walks a
     claude process's parent chain to whichever terminal is hosting it --
     the same lookup terminal-notifier's -activate uses for the banner's
     Show button. Always available.
  2. THE TAB. Terminal.app only; its AppleScript dictionary exposes tabs
     and their ttys. iTerm2 models sessions differently and VS Code
     exposes no addressable terminal tab, so both fall back to (1).

Best-effort by nature: the hook payload carries no PID, so with several
sessions waiting there is no way to know which one fired -- the same
limitation find_terminal_app_bundle_for_claude_code documents.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Callable, Optional

log = logging.getLogger(__name__)

TERMINAL_APP_BUNDLE_ID = "com.apple.Terminal"


def _signal_dirs() -> dict:
    """Which flag directory names the session responsible for each state.

    Pink-2026-09-01: originally only the wave could take you anywhere.
    Pink asked for the same on every active sprite -- "except idle/drowsy/
    sleeping, because they mean she's doing nothing" -- and every active
    state already has a directory naming who caused it, so the same
    session -> project -> process -> tty -> tab chain covers them all.

    Resting states are absent by design, not by omission: with nothing
    running there is no window a double-click could honestly raise, and
    yanking one to the front would be worse than doing nothing.
    """
    from . import watcher as w
    return {
        "working":         w.CLAUDE_TURN_ACTIVE_DIR,
        "thinking":        w.CLAUDE_TURN_ACTIVE_DIR,
        "approval_needed": w.CLAUDE_AWAITING_INPUT_DIR,
        "celebrating":     w.CLAUDE_TASK_COMPLETE_DIR,
        "grooving":        w.CLAUDE_FINISHED_DIR,
        # Pink-2026-09-16: a concerned face means a Claude turn just failed
        # on an API error -- double-click should take you to THAT session's
        # terminal, same session->tty->tab chain as the others. Keyed by the
        # claude_failed flag the StopFailure hook writes. (A Codex-sourced
        # concern has no per-session flag dir, so take_me_there falls through
        # to "resting" there -- acknowledge_concern still calms it. That is
        # only true because _state_fresh_sec bounds the lookup: see below.)
        "concerned":       w.CLAUDE_FAILED_DIR,
    }


STATE_SIGNAL_DIRS = _signal_dirs()

# States where we refuse to guess a window: if the responsible session cannot
# be identified, take-me-there does nothing rather than raising a Claude tab
# on spec.
#
# `concerned` qualifies because Codex raises it too and a Codex failure leaves
# no per-session flag anywhere, so "nothing fresh" can mean "no Claude session
# was involved at all" -- blind-raising a Claude window is then not imprecise,
# it is the wrong window, the exact bug this module exists to prevent. The
# watcher's _apply_failure_override prefers Claude over Codex, so a Codex-
# sourced concern really does imply no fresh Claude flag.
#
# DELIBERATELY NOT exhaustive. `working`, `thinking` and `celebrating` are
# multi-source too (working_evidence_merged/streaming_merged OR in
# codex_shell_active/codex_file_active/codex_streaming; celebrating fires on
# codex_celebrating or a Git commit), and `approval_needed` is effectively
# bounded by the 120s _CLAUDE_SESSION_SNOOZE_SEC rather than by staleness --
# so all of them can in principle raise the wrong window the same way. They
# are left out on purpose: the blind fallback there is Pink's documented
# choice ("better to raise the right app than to do nothing at all"), and
# flipping it is a UX call, not a bug fix. Widening this set means revisiting
# that test, deliberately.
_MULTI_SOURCE_STATES = frozenset({"concerned"})

# Slack for the ONE clock that actually feeds this decision. take_me_there
# reads PetApi._latest.state, which the watcher thread refreshes every
# POLL_INTERVAL_SEC -- the frontend's own poll never enters it. So the state we
# are handed is at most one tick stale, and two ticks of grace covers that.
#
# Kept deliberately tight: every second of grace is a second in which focus
# accepts a flag the watcher has already rejected, which re-opens the
# cross-source hijack this bound exists to close. An earlier revision used a
# flat 5s reasoned from the on-screen sprite lag -- the wrong clock, and 4s
# wider than the real one needs.
_FRESHNESS_GRACE_SEC = 2 * 1.0  # 2 * watcher.POLL_INTERVAL_SEC (import-cycle-safe)


def _state_fresh_sec(state: str) -> Optional[float]:
    """How recent a flag must be to still count as the cause of `state`.

    Take-me-there must follow the SAME signal that produced the face, so
    these mirror the fresh windows the watcher itself used when it decided
    the state -- they are not an independent policy, and deliberately not
    new magic numbers.

    Pink-2026-09-19: without this, focus read the flag dirs with no freshness
    rule at all while the watcher honoured one, so the two could disagree.
    The flag dirs are only *deleted* on their stale sweep (2h), far longer
    than any "still counts" window, which produced the exact wrong-window
    class this module exists to fix. Worked example: a Claude session fails
    at T. At T+400s the flag is no longer fresh so she stops looking
    concerned, but ~/.squid-pet/claude_failed/<sid> is still on disk. At
    T+600s Codex fails, so she is concerned again -- from a Codex signal.
    Double-clicking used to read that stale *Claude* flag and raise the wrong
    session's tab. Bounded, the lookup finds nothing fresh, returns None, and
    focus_for_state falls through to "resting" -- which is what the
    STATE_SIGNAL_DIRS docstring above always claimed happened.

    SCOPE IS DELIBERATELY `concerned` ONLY, and None (no window) everywhere
    else. `working`, `thinking`, `celebrating` and `grooving` carry the same
    latent bug -- their flags outlive their "still counts" window by up to the
    2h sweep, and every one of them can also be raised by Codex or a Git
    commit with no Claude flag at all -- but bounding them is not a freshness
    change. It collides with a deliberate, tested UX choice: when no session
    can be named she should still raise *a* window rather than do nothing
    (test_an_active_state_with_no_flag_still_raises_the_app). Bounding them
    without settling that first only converts a wrong-window raise into a less
    precise one, so it is left as a known gap on the PR rather than
    half-fixed here. See _MULTI_SOURCE_STATES.
    """
    from . import watcher as w
    if state == "concerned":
        # Plus a grace margin, because the face LAGS the flag: the watcher
        # re-evaluates on a POLL_INTERVAL_SEC tick and the frontend then polls
        # state.json, so she is still visibly concerned for a second or two
        # after the flag ages out. Bounding at exactly the watcher's window
        # would make a double-click in that gap do nothing at all -- strictly
        # worse than the old fallback. The cross-source case this bound exists
        # for is minutes wide, so a few seconds of slack costs it nothing.
        return w.CLAUDE_FAILED_FRESH_SEC + _FRESHNESS_GRACE_SEC
    return None


def _freshest_in(dir_path: str, fresh_sec: Optional[float] = None) -> Optional[str]:
    """Newest flag filename (a session id) in a signal directory.

    Freshest rather than first: whichever session most recently caused the
    state is the one you are reacting to.

    When fresh_sec is given, entries older than that are ignored -- excluded
    from consideration, never deleted; pruning belongs to the watcher's stale
    sweep, and this module must stay read-only so a double-click can never
    destroy state the watcher is still reasoning about.
    """
    import os
    import time
    try:
        names = os.listdir(dir_path)
    except OSError:
        return None
    now = time.time()
    best: tuple[float, str] | None = None
    for name in names:
        if name.startswith("."):
            continue
        try:
            mtime = os.stat(os.path.join(dir_path, name)).st_mtime
        except OSError:
            continue
        if fresh_sec is not None and (now - mtime) > fresh_sec:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, name)
    return best[1] if best else None


def freshest_waiting_session() -> Optional[str]:
    """Session id of the most recently raised wave."""
    from .watcher import CLAUDE_AWAITING_INPUT_DIR
    return _freshest_in(CLAUDE_AWAITING_INPUT_DIR)


def waiting_session_tty() -> Optional[str]:
    """Controlling terminal of the session that is actually waiting.

    Pink-2026-09-01: resolved through THAT session rather than "whichever
    claude process turns up first". A session's project directory
    identifies its process (watcher.claude_session_tty), which is what
    makes this correct when several sessions are running -- the hook
    payload's missing PID otherwise leaves it guessing, the same
    limitation find_terminal_app_bundle_for_claude_code documents.

    Falls back to any live process's tty, which is exactly right in the
    single-session case and no worse than the old behaviour otherwise.
    """
    try:
        from .watcher import claude_session_tty, find_claude_code_processes
    except Exception:
        return None
    sid = freshest_waiting_session()
    if sid:
        try:
            tty = claude_session_tty(sid)
            if tty:
                return tty
        except Exception:
            pass
    try:
        for proc in find_claude_code_processes():
            try:
                tty = proc.terminal()
            except Exception:
                continue
            if tty:
                return tty
    except Exception:
        pass
    return None


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def build_terminal_focus_script(tty: str, *, require_match: bool = False) -> str:
    """AppleScript raising the Terminal tab whose tty matches, and saying
    which it managed. Pure string building so the interesting part is
    testable without driving a real window."""
    t = _escape_applescript(tty)
    activate_before = "" if require_match else "    activate\n"
    activate_match = "                activate\n" if require_match else ""
    unmatched = "none" if require_match else "app-only"
    return (
        'tell application "Terminal"\n'
        f"{activate_before}"
        "    repeat with w in windows\n"
        "        repeat with tb in tabs of w\n"
        f'            if (tty of tb as text) is "{t}" then\n'
        f"{activate_match}"
        "                set index of w to 1\n"
        "                set selected tab of w to tb\n"
        '                return "matched"\n'
        "            end if\n"
        "        end repeat\n"
        "    end repeat\n"
        f'    return "{unmatched}"\n'
        "end tell\n"
    )


def build_app_activate_script(bundle_id: str) -> str:
    """Fallback: raise the hosting app without picking a window."""
    return f'tell application id "{_escape_applescript(bundle_id)}" to activate\n'


def _focus_process_owner(owner: dict, run=None, still_current=None) -> str:
    """Activate only an exact live process's Terminal tab."""
    import psutil

    from . import codex_turns, watcher
    try:
        if not isinstance(owner, dict) or not codex_turns.owner_alive(owner):
            return "none"
        proc = psutil.Process(owner['pid'])
        if proc.create_time() != owner.get('created'):
            return "none"
        tty = proc.terminal()
        if not tty:
            return "none"
        cur = proc
        bundle = None
        for _ in range(10):
            if cur is None:
                break
            bundle = (watcher._TERMINAL_APP_BUNDLE_IDS.get(cur.name())
                      or watcher._bundle_id_from_exe_path(cur.exe()))
            if bundle:
                break
            cur = cur.parent()
        # No exact-tab integration for other hosts yet. Raising their default
        # window could send the user to another inactive session again.
        if bundle != TERMINAL_APP_BUNDLE_ID:
            return "none"
        if not codex_turns.owner_alive(owner) or (still_current is not None and not still_current()):
            return "none"
        script = build_terminal_focus_script(tty, require_match=True)
        runner = run if run is not None else _run_osascript
        result = runner(script)
        return "matched" if result and result.strip() == "matched" else "none"
    except (OSError, ValueError, TypeError, KeyError, psutil.Error):
        return "none"


def _focus_codex_approval(requests: list[str], run=None) -> str:
    """Resolve the exact requesting process; never borrow a Claude session."""
    import json
    from pathlib import Path

    from . import codex_turns, watcher
    directory = Path(watcher.CODEX_AWAITING_INPUT_DIR)
    try:
        request = max(requests, key=lambda name: (directory / name).stat().st_mtime)
        owner_path = directory / ('.owner.' + request)
        if not owner_path.exists():
            owner_path = codex_turns.TURN_DIR / '.'.join(request.split('.')[:2])
        if owner_path.stat().st_size > 4096:
            return "none"
        owner = json.loads(owner_path.read_text())
        return _focus_process_owner(owner, run,
            still_current=lambda: request in watcher.codex_requests_awaiting_input())
    except (OSError, ValueError, TypeError):
        return "none"


def focus_for_snapshot(snapshot, run=None) -> str:
    """Navigate using the source that won this state, never a new global guess."""
    if snapshot.state in {'idle', 'sleeping', 'drowsy', 'stretch'}:
        return "resting"
    target = snapshot.focus_target
    if not target:
        return "none"
    if target.get('agent') == 'codex' and target.get('request'):
        return _focus_codex_approval([target['request']], run)
    if target.get('agent') == 'codex' and target.get('owner'):
        return _focus_process_owner(target['owner'], run)
    if target.get('agent') == 'claude':
        # Resolve only Claude's signal directory. The snapshot may predate a
        # new Codex request, which must not steal a Claude-origin click.
        return _focus_claude_state(snapshot.state, run, session=target.get('session'))
    return "none"


def _named_if_fresh(dir_path: str, name: str,
                    fresh_sec: Optional[float]) -> Optional[str]:
    """Return `name` if its flag is present in `dir_path` and (when fresh_sec
    is given) still within that window; otherwise None. Read-only, like
    _freshest_in -- pruning belongs to the watcher."""
    import os
    import time
    try:
        mtime = os.stat(os.path.join(dir_path, name)).st_mtime
    except OSError:
        return None
    if fresh_sec is not None and (time.time() - mtime) > fresh_sec:
        return None
    return name


def _focus_claude_state(state: str,
                        run: Optional[Callable[[str], Optional[str]]] = None,
                        session: Optional[str] = None) -> str:
    signal_dir = STATE_SIGNAL_DIRS.get(state)
    if signal_dir is None:
        return "resting"
    # Unbounded for every state but `concerned` (see _state_fresh_sec), so
    # this stays exactly the pre-existing lookup everywhere else: no
    # behaviour change, and no second pass that could only ever return the
    # same answer.
    fresh_sec = _state_fresh_sec(state)
    # When the winning state named its exact session (a StopFailure concern),
    # honour THAT session while its flag is still fresh -- a later failure in
    # another session must not move the click to its newer flag. If that
    # session's flag has since gone (replied/aged out), fall back to freshest.
    sid = _named_if_fresh(signal_dir, session, fresh_sec) if session else None
    if sid is None:
        sid = _freshest_in(signal_dir, fresh_sec)
    tty = None
    bundle = None
    if sid:
        try:
            from .watcher import _terminal_app_bundle_for_proc, claude_session_proc
            # Resolve BOTH the tab (tty) and the app (bundle) through the one
            # session that caused the state. Resolving the app separately, as
            # _raise's session-blind fallback does, can name a different
            # session's host when several run in different apps -- the
            # Pink-2026-09-16 bug where a failed Cursor turn sent the
            # concerned double-click to a Terminal window instead.
            #
            # Pink-2026-09-24: resolve the session's PROCESS once and derive
            # both facts from it. claude_session_tty() and
            # find_terminal_app_bundle_for_session() each re-ran
            # claude_session_proc() independently, so a session that moved (or
            # a process list that changed between the two lookups) could give a
            # tty and a bundle that name different sessions.
            proc = claude_session_proc(sid)
            if proc is not None:
                try:
                    tty = proc.terminal()
                except Exception:
                    tty = None
                bundle = _terminal_app_bundle_for_proc(proc)
        except Exception:
            tty = None
            bundle = None
    if sid is None and state in _MULTI_SOURCE_STATES:
        # NOTHING fresh named a session, so for a multi-source state there is
        # no evidence a Claude session was involved at all -- the face can
        # only have come from the other source. Blind-raising a Claude window
        # here is not imprecise, it is the wrong window.
        #
        # Deliberately narrower than "tty is None": a sid that resolves but
        # whose PROCESS has since exited is a different case -- a Claude
        # session demonstrably did just fail, we simply cannot find its tab,
        # and raising the right app is a reasonable best effort there. That
        # case is this feature author's documented choice, pinned by
        # test_take_me_there_falls_back_to_session_blind_bundle_when_unresolved.
        return "resting"
    if tty is None:
        # PRE-EXISTING, and it really is a guess: _any_claude_tty() returns
        # whichever live claude turns up first, and bundle stays None so
        # _raise blind-resolves the app too. With another session alive this
        # selects ITS tab and reports "matched" -- confidently taking you to an
        # unrelated healthy session. Untouched here because it is the feature
        # author's documented choice ("better to raise the right app than to do
        # nothing at all", pinned by
        # test_take_me_there_falls_back_to_session_blind_bundle_when_unresolved
        # and test_an_active_state_with_no_flag_still_raises_the_app), and
        # narrowing it is the same guess-vs-nothing product call this change
        # deliberately does not make. Listed as a known gap on the PR.
        tty = _any_claude_tty()
    return _raise(tty, run, bundle=bundle)


def focus_for_state(state: str,
                    run: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """Bring the window responsible for `state` to the front.

    Returns "matched" (that tab is now front), "app-only" (right app,
    unknown tab), "none" (no terminal identified) or "resting" (nothing to
    open -- idle, drowsy, sleeping, or any state we have no signal for).
    """
    if state == "approval_needed":
        from . import watcher
        requests = watcher.codex_requests_awaiting_input()
        if requests:
            return _focus_codex_approval(requests, run)
        if not watcher.claude_sessions_awaiting_input():
            return "none"  # The displayed wave may outlive the actual request.
    return _focus_claude_state(state, run)


def _any_claude_tty() -> Optional[str]:
    try:
        from .watcher import find_claude_code_processes
        for proc in find_claude_code_processes():
            try:
                tty = proc.terminal()
            except Exception:
                continue
            if tty:
                return tty
    except Exception:
        pass
    return None


def _raise(tty: Optional[str],
           run: Optional[Callable[[str], Optional[str]]] = None,
           bundle: Optional[str] = None) -> str:
    """Raise the window for a tty, hosted by `bundle`.

    `bundle` is the app the caller already resolved through the specific
    responsible session; when it is None (session gone, or a source with no
    per-session process such as Codex) we fall back to the session-blind
    first-found lookup -- no worse than before, but never overriding a
    correct per-session answer with it.
    """
    runner = run if run is not None else _run_osascript
    if bundle is None:
        try:
            from .watcher import find_terminal_app_bundle_for_claude_code
            bundle = find_terminal_app_bundle_for_claude_code()
        except Exception:
            bundle = None
    if bundle == TERMINAL_APP_BUNDLE_ID and tty:
        out = runner(build_terminal_focus_script(tty))
        if out is not None:
            return out.strip() or "app-only"
    if bundle:
        runner(build_app_activate_script(bundle))
        return "app-only"
    return "none"


def focus_waiting_session(run: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """Bring the waiting session to the front.

    Returns what it achieved: "matched" (that tab is now front), "app-only"
    (right app, unknown tab), or "none". `run` is injectable so tests never
    raise a real window.
    """
    from . import watcher
    requests = watcher.codex_requests_awaiting_input()
    if requests:
        return _focus_codex_approval(requests, run)
    runner = run if run is not None else _run_osascript
    try:
        from .watcher import find_terminal_app_bundle_for_claude_code
        bundle = find_terminal_app_bundle_for_claude_code()
    except Exception:
        bundle = None

    tty = waiting_session_tty()
    if bundle == TERMINAL_APP_BUNDLE_ID and tty:
        out = runner(build_terminal_focus_script(tty))
        if out is not None:
            return out.strip() or "app-only"
    if bundle:
        runner(build_app_activate_script(bundle))
        return "app-only"
    return "none"


def _run_osascript(script: str) -> Optional[str]:
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=5)
        return r.stdout
    except Exception as e:
        log.warning("focus (osascript) failed: %s", e)
        return None
