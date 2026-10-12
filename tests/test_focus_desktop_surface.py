"""Phase B -- "take me there" for a desktop-app (Code tab) session.

The desktop app runs its own bundled CLI with no terminal. Resolving such a
session through the tty chain found no tty and fell through to "any live claude
process's tty" -- silently raising whatever Terminal tab happened to be open.
A session recorded as surface "local-agent" now raises the Claude desktop app
by bundle id and stops: there is no public way to pick a Code tab, so none is
guessed. The terminal ("cli") path -- and "unknown" / unrecorded / "sdk-cli",
which are treated exactly like it -- is unchanged.

Hermetic: every flag dir is under tmp_path, and the osascript runner is
injected, so no real window is ever raised.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from squid_pet import focus, watcher
from squid_pet.watcher import PetState

DESKTOP = "com.anthropic.claudefordesktop"


class _Proc:
    def __init__(self, tty):
        self._tty = tty

    def terminal(self):
        return self._tty


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Isolated awaiting / turn-active / failed / surface dirs, wired into both
    the watcher constants and focus's import-time snapshot of them."""
    out = {}
    for state, attr in (("approval_needed", "CLAUDE_AWAITING_INPUT_DIR"),
                        ("working", "CLAUDE_TURN_ACTIVE_DIR"),
                        ("concerned", "CLAUDE_FAILED_DIR")):
        d = tmp_path / attr
        d.mkdir()
        monkeypatch.setattr(watcher, attr, str(d))
        monkeypatch.setitem(focus.STATE_SIGNAL_DIRS, state, str(d))
        out[state] = d
    surf = Path(watcher.CLAUDE_SESSION_SURFACE_DIR)  # conftest: under tmp_path
    surf.mkdir(parents=True, exist_ok=True)
    out["surface"] = surf
    monkeypatch.setattr(watcher, "codex_requests_awaiting_input", lambda: [])
    return out


def _flag(d: Path, sid: str, mtime: float) -> None:
    (d / sid).write_text("x")
    os.utime(d / sid, (mtime, mtime))


def _surface(dirs, sid: str, surface: str) -> None:
    (dirs["surface"] / sid).write_text(surface)


def _terminal_world(monkeypatch, *, tty_by_sid: dict, stray_tty="/dev/ttys999"):
    """A terminal session per sid (resolved by its own tty), plus a stray live
    claude process with an unrelated tab -- the one the old fallback raised."""
    monkeypatch.setattr(watcher, "claude_session_proc",
                        lambda sid: _Proc(tty_by_sid[sid]) if sid in tty_by_sid else None)
    monkeypatch.setattr(watcher, "_terminal_app_bundle_for_proc",
                        lambda proc: focus.TERMINAL_APP_BUNDLE_ID)
    monkeypatch.setattr(watcher, "find_claude_code_processes",
                        lambda: [_Proc(stray_tty)])
    monkeypatch.setattr(watcher, "find_terminal_app_bundle_for_claude_code",
                        lambda: focus.TERMINAL_APP_BUNDLE_ID)


def _no_tty_resolution(monkeypatch):
    """Fail loudly if a desktop click goes anywhere near the tty chain."""
    def fail(*_a, **_k):
        pytest.fail("a desktop session must not be resolved through the tty chain")
    monkeypatch.setattr(watcher, "claude_session_proc", fail)
    monkeypatch.setattr(watcher, "claude_session_tty", fail)
    monkeypatch.setattr(watcher, "find_claude_code_processes", fail)
    monkeypatch.setattr(watcher, "find_terminal_app_bundle_for_claude_code", fail)
    monkeypatch.setattr(focus, "_any_claude_tty", fail)


def _recorder():
    seen: list[str] = []

    def run(script):
        seen.append(script)
        return "matched" if "tty of tb" in script else ""
    return seen, run


# ── the desktop session raises the desktop app, and nothing else ──────────
def test_desktop_wave_raises_the_desktop_app_and_never_a_terminal_tab(dirs, monkeypatch):
    _flag(dirs["approval_needed"], "sess-d", 2000)
    _surface(dirs, "sess-d", "local-agent")
    _no_tty_resolution(monkeypatch)
    seen, run = _recorder()
    snap = PetState(state="approval_needed", focus_target={"agent": "claude"})
    assert focus.focus_for_snapshot(snap, run) == "app-only"
    assert seen == [focus.build_app_activate_script(DESKTOP)], \
        "must use the existing app-activate idiom, once, for the desktop bundle"


def test_desktop_constants_agree():
    from squid_pet import doctor
    assert focus.CLAUDE_DESKTOP_BUNDLE_ID == watcher.CLAUDE_DESKTOP_BUNDLE_ID == DESKTOP
    assert doctor.CLAUDE_DESKTOP_BUNDLE_ID == DESKTOP


@pytest.mark.parametrize("state,target_extra", [
    ("concerned", {}),
    ("working", {"session": "sess-d"}),
    ("thinking", {"session": "sess-d"}),
])
def test_other_states_naming_a_desktop_session_raise_the_desktop_app(
        dirs, monkeypatch, state, target_extra):
    import time
    _flag(dirs["concerned"], "sess-d", time.time())
    _flag(dirs["working"], "sess-d", time.time())
    _surface(dirs, "sess-d", "local-agent")
    _no_tty_resolution(monkeypatch)
    seen, run = _recorder()
    snap = PetState(state=state, focus_target={"agent": "claude", **target_extra})
    assert focus.focus_for_snapshot(snap, run) == "app-only"
    assert seen == [focus.build_app_activate_script(DESKTOP)]


# ── terminal / unknown sessions: today's path, unchanged ──────────────────
@pytest.mark.parametrize("surface", ["cli", "unknown", "sdk-cli", "garbage", None])
def test_non_desktop_sessions_keep_the_exact_tty_path(dirs, monkeypatch, surface):
    _flag(dirs["approval_needed"], "sess-t", 2000)
    if surface is not None:
        _surface(dirs, "sess-t", surface)
    _terminal_world(monkeypatch, tty_by_sid={"sess-t": "/dev/ttys004"})
    seen, run = _recorder()
    snap = PetState(state="approval_needed", focus_target={"agent": "claude"})
    assert focus.focus_for_snapshot(snap, run) == "matched"
    assert len(seen) == 1 and '"/dev/ttys004"' in seen[0]
    assert DESKTOP not in seen[0]


# ── mixed: a terminal session and a desktop session waiting together ──────
def test_mixed_wave_desktop_freshest_raises_the_desktop_app(dirs, monkeypatch):
    _flag(dirs["approval_needed"], "sess-t", 1000)
    _flag(dirs["approval_needed"], "sess-d", 2000)
    _surface(dirs, "sess-t", "cli")
    _surface(dirs, "sess-d", "local-agent")
    _terminal_world(monkeypatch, tty_by_sid={"sess-t": "/dev/ttys004"})
    seen, run = _recorder()
    snap = PetState(state="approval_needed", focus_target={"agent": "claude"})
    assert focus.focus_for_snapshot(snap, run) == "app-only"
    assert seen == [focus.build_app_activate_script(DESKTOP)]


def test_mixed_wave_terminal_freshest_raises_that_terminals_own_tab(dirs, monkeypatch):
    _flag(dirs["approval_needed"], "sess-t", 2000)
    _flag(dirs["approval_needed"], "sess-d", 1000)
    _surface(dirs, "sess-t", "cli")
    _surface(dirs, "sess-d", "local-agent")
    _terminal_world(monkeypatch, tty_by_sid={"sess-t": "/dev/ttys004"})
    seen, run = _recorder()
    snap = PetState(state="approval_needed", focus_target={"agent": "claude"})
    assert focus.focus_for_snapshot(snap, run) == "matched"
    assert len(seen) == 1 and '"/dev/ttys004"' in seen[0]
    assert all(DESKTOP not in s for s in seen)


# ── the legacy focus_waiting_session / waiting_session_tty entry points ───
def test_focus_waiting_session_raises_the_desktop_app_for_a_desktop_wave(dirs, monkeypatch):
    _flag(dirs["approval_needed"], "sess-d", 2000)
    _surface(dirs, "sess-d", "local-agent")
    _no_tty_resolution(monkeypatch)
    seen, run = _recorder()
    assert focus.focus_waiting_session(run) == "app-only"
    assert seen == [focus.build_app_activate_script(DESKTOP)]


def test_focus_waiting_session_terminal_wave_is_unchanged(dirs, monkeypatch):
    _flag(dirs["approval_needed"], "sess-t", 2000)
    _surface(dirs, "sess-t", "cli")
    monkeypatch.setattr(watcher, "claude_session_tty",
                        lambda sid: "/dev/ttys004" if sid == "sess-t" else None)
    monkeypatch.setattr(watcher, "find_terminal_app_bundle_for_claude_code",
                        lambda: focus.TERMINAL_APP_BUNDLE_ID)
    seen, run = _recorder()
    assert focus.focus_waiting_session(run) == "matched"
    assert '"/dev/ttys004"' in seen[0]


def test_waiting_session_tty_never_borrows_a_stray_tab_for_a_desktop_session(
        dirs, monkeypatch):
    """The actual bug: a desktop session has no tty, and the 'any live claude
    process' fallback handed back an unrelated Terminal tab."""
    _flag(dirs["approval_needed"], "sess-d", 2000)
    _surface(dirs, "sess-d", "local-agent")
    monkeypatch.setattr(watcher, "claude_session_tty", lambda sid: None)
    monkeypatch.setattr(watcher, "find_claude_code_processes",
                        lambda: [_Proc("/dev/ttys999")])
    assert focus.waiting_session_tty() is None


def test_waiting_session_tty_fallback_is_unchanged_for_terminal_sessions(dirs, monkeypatch):
    _flag(dirs["approval_needed"], "sess-t", 2000)
    _surface(dirs, "sess-t", "cli")
    monkeypatch.setattr(watcher, "claude_session_tty", lambda sid: None)
    monkeypatch.setattr(watcher, "find_claude_code_processes",
                        lambda: [_Proc("/dev/ttys999")])
    assert focus.waiting_session_tty() == "/dev/ttys999"


# ── host-app resolution: the bundled CLI is never the host ────────────────
class _ChainProc:
    def __init__(self, name, exe, parent=None):
        self._name, self._exe, self._parent = name, exe, parent

    def name(self):
        return self._name

    def exe(self):
        return self._exe

    def parent(self):
        return self._parent


def test_a_desktop_spawned_claude_resolves_to_the_desktop_app(monkeypatch):
    """A working-state click resolved through the shell OWNER process (no
    session id) used to stop at the bundled CLI's own claude.app and activate
    com.anthropic.claude-code -- a CLI, not a window. The walk must skip it and
    reach the desktop app that spawned it."""
    bundled = ("/Users/you/Library/Application Support/Claude/claude-code/2.1.92/"
               "claude.app/Contents/MacOS/claude")
    app = "/Applications/Claude.app/Contents/MacOS/Claude"
    plist = {bundled: "com.anthropic.claude-code", app: DESKTOP}
    monkeypatch.setattr(watcher, "_bundle_id_from_exe_path", lambda exe: plist.get(exe))
    proc = _ChainProc("claude", bundled, _ChainProc("Claude", app))
    assert watcher._terminal_app_bundle_for_proc(proc) == DESKTOP


def test_a_terminal_claude_still_resolves_to_its_terminal(monkeypatch):
    monkeypatch.setattr(watcher, "_bundle_id_from_exe_path", lambda exe: None)
    proc = _ChainProc("claude", "/Users/you/.local/bin/claude",
                      _ChainProc("zsh", "/bin/zsh",
                                 _ChainProc("Terminal", "/System/Applications/"
                                            "Utilities/Terminal.app/Contents/MacOS/Terminal")))
    assert watcher._terminal_app_bundle_for_proc(proc) == focus.TERMINAL_APP_BUNDLE_ID


def test_working_click_via_a_desktop_shell_owner_raises_the_desktop_app(monkeypatch):
    """End to end through the owner path: the stray tty the blind fallback
    finds is ignored because the resolved host is the desktop app, not
    Terminal."""
    bundled = ("/Users/you/Library/Application Support/Claude/claude-code/2.1.92/"
               "claude.app/Contents/MacOS/claude")
    app = "/Applications/Claude.app/Contents/MacOS/Claude"
    plist = {bundled: "com.anthropic.claude-code", app: DESKTOP}
    monkeypatch.setattr(watcher, "_bundle_id_from_exe_path", lambda exe: plist.get(exe))

    class _OwnerProc(_ChainProc):
        def terminal(self):
            return None

    owner_proc = _OwnerProc("claude", bundled, _ChainProc("Claude", app))
    monkeypatch.setattr(focus, "_claude_owner_proc", lambda owner: owner_proc)
    monkeypatch.setattr(focus, "_any_claude_tty", lambda: "/dev/ttys999")
    seen, run = _recorder()
    snap = PetState(state="working",
                    focus_target={"agent": "claude", "owner": {"pid": 1, "created": 1.0}})
    assert focus.focus_for_snapshot(snap, run) == "app-only"
    assert seen == [focus.build_app_activate_script(DESKTOP)]


# ── review fix 1: the owner_resolved path must honour the desktop surface ──
# The trigger: a working/thinking click resolved through a LIVE shell owner,
# whose host-app walk returns None (e.g. the desktop app is gone and the
# bundled claude was reparented to launchd). The old flow then took
# _any_claude_tty() -- another session's tab -- and blind-resolved the bundle
# to Terminal.app, confidently raising a stranger's Terminal tab.
_BUNDLED = ("/Users/you/Library/Application Support/Claude/claude-code/2.1.92/"
            "claude.app/Contents/MacOS/claude")


class _OwnerProc(_ChainProc):
    def __init__(self, exe, tty=None):
        super().__init__("claude", exe, None)
        self._tty = tty

    def terminal(self):
        return self._tty


def _owner_world(monkeypatch, owner_proc):
    """Owner resolves; the host walk finds nothing; another terminal session
    is alive with its own tab, and the blind bundle lookup says Terminal."""
    monkeypatch.setattr(focus, "_claude_owner_proc", lambda owner: owner_proc)
    monkeypatch.setattr(watcher, "_terminal_app_bundle_for_proc", lambda proc: None)
    monkeypatch.setattr(watcher, "_bundle_id_from_exe_path",
                        lambda exe: "com.anthropic.claude-code" if exe == _BUNDLED else None)
    monkeypatch.setattr(watcher, "find_claude_code_processes",
                        lambda: [_Proc("/dev/ttys999")])
    monkeypatch.setattr(watcher, "find_terminal_app_bundle_for_claude_code",
                        lambda: focus.TERMINAL_APP_BUNDLE_ID)


@pytest.mark.parametrize("state", ["working", "thinking"])
def test_desktop_owner_with_no_host_raises_the_desktop_app_not_a_strangers_tab(
        monkeypatch, state):
    _owner_world(monkeypatch, _OwnerProc(_BUNDLED, tty=None))
    stray = []
    real_any = focus._any_claude_tty
    monkeypatch.setattr(focus, "_any_claude_tty",
                        lambda: stray.append(1) or real_any())
    seen, run = _recorder()
    snap = PetState(state=state,
                    focus_target={"agent": "claude", "owner": {"pid": 1, "created": 1.0}})
    assert focus.focus_for_snapshot(snap, run) == "app-only"
    assert seen == [focus.build_app_activate_script(DESKTOP)]
    assert all("tty of tb" not in s for s in seen), "a Terminal script ran"
    assert stray == [], "_any_claude_tty() must never be reached for a desktop session"


def test_owner_path_honours_a_recorded_desktop_session_id(dirs, monkeypatch):
    """When the target also names its session, a recorded desktop surface wins
    even though the owner process resolved and looks like nothing in particular."""
    _surface(dirs, "sess-d", "local-agent")
    _owner_world(monkeypatch, _OwnerProc("/opt/odd/claude", tty=None))
    monkeypatch.setattr(focus, "_any_claude_tty",
                        lambda: pytest.fail("reached the blind tty fallback"))
    seen, run = _recorder()
    snap = PetState(state="working", focus_target={
        "agent": "claude", "session": "sess-d", "owner": {"pid": 1, "created": 1.0}})
    assert focus.focus_for_snapshot(snap, run) == "app-only"
    assert seen == [focus.build_app_activate_script(DESKTOP)]


def test_terminal_owner_with_no_host_keeps_the_documented_blind_fallback(monkeypatch):
    """No regression: a TERMINAL owner whose walk finds nothing still takes the
    pre-existing blind fallback (_any_claude_tty + session-blind bundle) --
    the documented choice this change deliberately does not narrow."""
    _owner_world(monkeypatch, _OwnerProc("/Users/you/.local/bin/claude", tty=None))
    seen, run = _recorder()
    snap = PetState(state="working",
                    focus_target={"agent": "claude", "owner": {"pid": 1, "created": 1.0}})
    assert focus.focus_for_snapshot(snap, run) == "matched"
    assert len(seen) == 1 and '"/dev/ttys999"' in seen[0]


def test_a_bundled_cli_run_from_a_terminal_keeps_the_tty_path(monkeypatch):
    """The bundled binary launched by hand in a terminal HAS a tty: that is a
    terminal session and keeps the exact-tab path."""
    _owner_world(monkeypatch, _OwnerProc(_BUNDLED, tty="/dev/ttys004"))
    monkeypatch.setattr(watcher, "_terminal_app_bundle_for_proc",
                        lambda proc: focus.TERMINAL_APP_BUNDLE_ID)
    seen, run = _recorder()
    snap = PetState(state="working",
                    focus_target={"agent": "claude", "owner": {"pid": 1, "created": 1.0}})
    assert focus.focus_for_snapshot(snap, run) == "matched"
    assert '"/dev/ttys004"' in seen[0]
