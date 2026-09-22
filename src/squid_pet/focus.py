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
    }


STATE_SIGNAL_DIRS = _signal_dirs()


def _freshest_in(dir_path: str) -> Optional[str]:
    """Newest flag filename (a session id) in a signal directory.

    Freshest rather than first: whichever session most recently caused the
    state is the one you are reacting to.
    """
    import os
    try:
        names = os.listdir(dir_path)
    except OSError:
        return None
    best: tuple[float, str] | None = None
    for name in names:
        if name.startswith("."):
            continue
        try:
            mtime = os.stat(os.path.join(dir_path, name)).st_mtime
        except OSError:
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
        return _focus_claude_state(snapshot.state, run)
    return "none"


def _focus_claude_state(state: str,
                        run: Optional[Callable[[str], Optional[str]]] = None) -> str:
    signal_dir = STATE_SIGNAL_DIRS.get(state)
    if signal_dir is None:
        return "resting"
    sid = _freshest_in(signal_dir)
    tty = None
    if sid:
        try:
            from .watcher import claude_session_tty
            tty = claude_session_tty(sid)
        except Exception:
            tty = None
    if tty is None:
        tty = _any_claude_tty()
    return _raise(tty, run)


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
           run: Optional[Callable[[str], Optional[str]]] = None) -> str:
    runner = run if run is not None else _run_osascript
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
