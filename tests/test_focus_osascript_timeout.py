"""focus._run_osascript's timeout: 2s per attempt, one retry on timeout only,
and no second script chained after a double timeout.

Pink-2026-10-18: live-daemon logs showed 116 raises, 113 fine at 0.6-0.8s and
3 timed out -- each time the FIRST call, with nothing else in flight. The 5s
cap was arbitrary, and after a timeout the code ran a SECOND osascript (the
app-activate fallback, also capped at 5s): in the 2026-10-12 incident that
made the user wait ~10s to land in the right app but the wrong tab.

Injected `run=` runners bypass _run_osascript entirely, so the retry is
tested by replacing subprocess.run. Every world here is stubbed -- flag dirs
under tmp_path, no real process lookup, no real osascript.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

import pytest

from squid_pet import focus, watcher
from squid_pet.watcher import PetState
from squid_pet.window import PetApi

TTY = "/dev/ttys042"
DESKTOP = "com.anthropic.claudefordesktop"


class _FakeRun:
    """Stand-in for subprocess.run. `script` is a list of outcomes, one per
    call: an exception instance to raise, or a stdout string to return."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        outcome = self.outcomes.pop(0) if self.outcomes else ""
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(cmd, 0, stdout=outcome, stderr="")

    @property
    def scripts(self) -> list[str]:
        return [c["cmd"][2] for c in self.calls]


def _timeout():
    return subprocess.TimeoutExpired(["osascript", "-e", "<script>"], 2)


@pytest.fixture
def fake_run(monkeypatch):
    def install(*outcomes):
        fake = _FakeRun(*outcomes)
        monkeypatch.setattr(focus.subprocess, "run", fake)
        return fake
    return install


class _Proc:
    def __init__(self, tty):
        self._tty = tty

    def terminal(self):
        return self._tty


@pytest.fixture
def terminal_wave(tmp_path, monkeypatch):
    """A Claude session waving from a Terminal.app tab with tty TTY."""
    d = tmp_path / "claude_awaiting_input"
    d.mkdir()
    (d / "sess-t").write_text("x")
    monkeypatch.setattr(watcher, "CLAUDE_AWAITING_INPUT_DIR", str(d))
    monkeypatch.setitem(focus.STATE_SIGNAL_DIRS, "approval_needed", str(d))
    monkeypatch.setattr(watcher, "codex_requests_awaiting_input", lambda: [])
    monkeypatch.setattr(watcher, "claude_sessions_awaiting_input", lambda: ["sess-t"])
    monkeypatch.setattr(watcher, "claude_session_proc",
                        lambda sid: _Proc(TTY) if sid == "sess-t" else None)
    monkeypatch.setattr(watcher, "claude_session_tty",
                        lambda sid: TTY if sid == "sess-t" else None)
    monkeypatch.setattr(watcher, "_terminal_app_bundle_for_proc",
                        lambda proc: focus.TERMINAL_APP_BUNDLE_ID)
    monkeypatch.setattr(watcher, "find_terminal_app_bundle_for_claude_code",
                        lambda: focus.TERMINAL_APP_BUNDLE_ID)
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [_Proc(TTY)])
    return PetState(state="approval_needed", focus_target={"agent": "claude"})


@pytest.fixture
def desktop_wave(tmp_path, monkeypatch):
    """A Claude session waving from the desktop app's Code tab. The tty chain
    fails loudly if anything reaches for it."""
    d = tmp_path / "claude_awaiting_input"
    d.mkdir()
    (d / "sess-d").write_text("x")
    monkeypatch.setattr(watcher, "CLAUDE_AWAITING_INPUT_DIR", str(d))
    monkeypatch.setitem(focus.STATE_SIGNAL_DIRS, "approval_needed", str(d))
    surf = Path(watcher.CLAUDE_SESSION_SURFACE_DIR)  # conftest: under tmp_path
    surf.mkdir(parents=True, exist_ok=True)
    (surf / "sess-d").write_text(watcher.CLAUDE_SURFACE_DESKTOP)
    monkeypatch.setattr(watcher, "codex_requests_awaiting_input", lambda: [])

    def fail(*_a, **_k):
        pytest.fail("a desktop session must not be resolved through the tty chain")
    for name in ("claude_session_proc", "claude_session_tty",
                 "find_claude_code_processes",
                 "find_terminal_app_bundle_for_claude_code"):
        monkeypatch.setattr(watcher, name, fail)
    monkeypatch.setattr(focus, "_any_claude_tty", fail)
    return PetState(state="approval_needed", focus_target={"agent": "claude"})


def _pet_api(snapshot):
    api = PetApi.__new__(PetApi)
    api._lock = threading.Lock()
    api._latest = snapshot
    api._hint_text = ""
    api._hint_seq = 0
    api._focus_fn = focus.focus_for_snapshot
    return api


# ─── _run_osascript itself ────────────────────────────────────────────────
def test_each_attempt_is_capped_at_two_seconds(fake_run):
    fake = fake_run("matched\n")
    focus._run_osascript(focus.build_terminal_focus_script(TTY))
    assert [c["timeout"] for c in fake.calls] == [2]


def test_one_timeout_then_success_retries_once_and_returns_the_success(fake_run):
    fake = fake_run(_timeout(), "matched\n")
    assert focus._run_osascript(focus.build_terminal_focus_script(TTY)) == "matched\n"
    assert len(fake.calls) == 2
    assert fake.scripts[0] == fake.scripts[1], "the retry re-runs the same script"


def test_a_first_time_success_is_not_retried(fake_run):
    fake = fake_run("matched\n")
    assert focus._run_osascript(focus.build_terminal_focus_script(TTY)) == "matched\n"
    assert len(fake.calls) == 1


@pytest.mark.parametrize("exc", [FileNotFoundError(2, "No such file"),
                                 OSError("spawn failed"), ValueError("bad")])
def test_a_non_timeout_failure_is_not_retried_and_still_returns_none(fake_run, exc):
    """A missing binary or a malformed call will not fix itself on a retry;
    the old None-returning contract is kept for it."""
    fake = fake_run(exc, "matched\n")
    assert focus._run_osascript(focus.build_terminal_focus_script(TTY)) is None
    assert len(fake.calls) == 1


# ─── Terminal path ────────────────────────────────────────────────────────
def test_a_double_timeout_reports_timeout_and_runs_no_fallback(fake_run, terminal_wave):
    fake = fake_run(_timeout(), _timeout(), "")
    assert focus.focus_for_snapshot(terminal_wave) == "timeout"
    assert len(fake.calls) == 2, "two tab-match attempts, then stop"
    assert all("tty of tb" in s for s in fake.scripts), \
        "the app-activate fallback must not run after a timeout"


def test_a_double_timeout_tells_the_user(fake_run, terminal_wave):
    fake_run(_timeout(), _timeout())
    api = _pet_api(terminal_wave)
    assert api.take_me_there() == {"status": "timeout", "state": "approval_needed"}
    assert api._hint_seq == 1
    assert "in time" in api._hint_text
    assert "on my way" not in api._hint_text, "must not promise arrival"


def test_one_timeout_then_success_still_lands_in_the_tab(fake_run, terminal_wave):
    fake = fake_run(_timeout(), "matched\n")
    assert focus.focus_for_snapshot(terminal_wave) == "matched"
    assert len(fake.calls) == 2


def test_a_script_that_ran_but_matched_no_tab_is_app_only_in_one_call(
        fake_run, terminal_wave):
    """The Terminal script activates Terminal itself and answers "app-only"
    when no tab's tty matches, so no second script is needed there."""
    fake = fake_run("app-only\n")
    assert focus.focus_for_snapshot(terminal_wave) == "app-only"
    assert len(fake.calls) == 1


def test_a_non_timeout_tab_failure_still_reaches_the_activate_fallback(
        fake_run, terminal_wave):
    fake = fake_run(OSError("spawn failed"), "")
    assert focus.focus_for_snapshot(terminal_wave) == "app-only"
    assert len(fake.calls) == 2
    assert fake.scripts[1] == focus.build_app_activate_script(focus.TERMINAL_APP_BUNDLE_ID)


def test_a_failed_activate_fallback_is_not_reported_as_app_only(fake_run, terminal_wave):
    """The fallback used to discard the activate's result and claim
    "app-only" whatever happened."""
    fake = fake_run(OSError("spawn failed"), OSError("spawn failed"))
    assert focus.focus_for_snapshot(terminal_wave) == "error"
    assert len(fake.calls) == 2


def test_a_timed_out_activate_fallback_reports_timeout(fake_run, terminal_wave):
    fake = fake_run(OSError("spawn failed"), _timeout(), _timeout(), "")
    assert focus.focus_for_snapshot(terminal_wave) == "timeout"
    assert len(fake.calls) == 3


def test_an_injected_runner_returning_none_still_reaches_the_activate_fallback(
        terminal_wave):
    """The injectable runner contract is unchanged: None from the tab script
    falls through to the activate script, as before."""
    seen: list[str] = []

    def run(script):
        seen.append(script)
        return None if "tty of tb" in script else ""
    assert focus.focus_for_snapshot(terminal_wave, run) == "app-only"
    assert seen[1] == focus.build_app_activate_script(focus.TERMINAL_APP_BUNDLE_ID)


def test_an_injected_activate_returning_none_is_not_app_only(terminal_wave):
    assert focus.focus_for_snapshot(terminal_wave, lambda s: None) == "error"


def test_focus_waiting_session_double_timeout_runs_no_fallback(fake_run, terminal_wave):
    fake = fake_run(_timeout(), _timeout(), "")
    assert focus.focus_waiting_session() == "timeout"
    assert len(fake.calls) == 2
    assert all("tty of tb" in s for s in fake.scripts)


# ─── Desktop path ─────────────────────────────────────────────────────────
def test_desktop_double_timeout_reports_timeout_with_no_other_script(
        fake_run, desktop_wave):
    fake = fake_run(_timeout(), _timeout(), "")
    assert focus.focus_for_snapshot(desktop_wave) == "timeout"
    assert fake.scripts == [focus.build_app_activate_script(DESKTOP)] * 2


def test_desktop_one_timeout_then_success_is_app_only(fake_run, desktop_wave):
    fake = fake_run(_timeout(), "")
    assert focus.focus_for_snapshot(desktop_wave) == "app-only"
    assert len(fake.calls) == 2


def test_desktop_failed_raise_is_not_reported_as_app_only(fake_run, desktop_wave):
    fake_run(OSError("spawn failed"))
    assert focus.focus_for_snapshot(desktop_wave) == "error"


def test_desktop_double_timeout_tells_the_user(fake_run, desktop_wave):
    fake_run(_timeout(), _timeout())
    api = _pet_api(desktop_wave)
    assert api.take_me_there()["status"] == "timeout"
    assert api._hint_seq == 1


# ─── Codex exact-tab path ─────────────────────────────────────────────────
def test_codex_owner_double_timeout_reports_timeout_not_none(fake_run, monkeypatch):
    """The Codex owner path catches OSError; the timeout must not be mistaken
    for one and silently become "none"."""
    import psutil

    from squid_pet import codex_turns

    class Proc:
        def create_time(self):
            return 50

        def terminal(self):
            return TTY

        def name(self):
            return "Terminal"

        def exe(self):
            return "/usr/local/bin/unknown"

        def parent(self):
            return None

    monkeypatch.setattr(codex_turns, "owner_alive", lambda data: data.get("pid") == 123)
    monkeypatch.setattr(psutil, "Process", lambda pid: Proc())
    monkeypatch.setitem(watcher._TERMINAL_APP_BUNDLE_IDS, "Terminal",
                        focus.TERMINAL_APP_BUNDLE_ID)
    fake = fake_run(_timeout(), _timeout(), "")
    snap = PetState(state="working",
                    focus_target={"agent": "codex", "owner": {"pid": 123, "created": 50}})
    assert focus.focus_for_snapshot(snap) == "timeout"
    assert len(fake.calls) == 2


# ─── Instrumentation ──────────────────────────────────────────────────────
def test_each_attempt_logs_one_line_without_the_script_or_tty(
        fake_run, terminal_wave, caplog):
    fake_run(_timeout(), "matched\n")
    with caplog.at_level(logging.INFO, logger="squid_pet.focus"):
        focus.focus_for_snapshot(terminal_wave)
    lines = [r for r in caplog.records if r.name == "squid_pet.focus"]
    assert len(lines) == 2, [r.getMessage() for r in lines]
    first, second = lines
    assert first.levelno == logging.WARNING
    assert second.levelno == logging.INFO
    for rec, attempt, outcome in ((first, 1, "timeout"), (second, 2, "ok")):
        msg = rec.getMessage()
        assert "kind=tab-match" in msg
        assert f"attempt={attempt}/2" in msg
        assert "elapsed_ms=" in msg
        assert f"outcome={outcome}" in msg
    for rec in caplog.records:
        msg = rec.getMessage()
        assert TTY not in msg and "ttys042" not in msg, msg
        assert "tell application" not in msg, msg
        assert os.path.expanduser("~") not in msg, msg


@pytest.mark.parametrize("script,kind", [
    (focus.build_terminal_focus_script(TTY), "tab-match"),
    (focus.build_terminal_focus_script(TTY, require_match=True), "tab-match"),
    (focus.build_app_activate_script(focus.TERMINAL_APP_BUNDLE_ID), "app-activate"),
    (focus.build_app_activate_script("com.example.editor"), "app-activate"),
    (focus.build_app_activate_script(DESKTOP), "desktop-raise"),
], ids=["tab", "tab-require-match", "terminal-activate", "other-activate", "desktop"])
def test_each_script_kind_is_named_in_its_log_line(fake_run, caplog, script, kind):
    fake_run("")
    with caplog.at_level(logging.INFO, logger="squid_pet.focus"):
        focus._run_osascript(script)
    assert [f"kind={kind}" in r.getMessage() for r in caplog.records] == [True]


def test_a_non_timeout_failure_logs_its_type_not_its_message(fake_run, caplog):
    fake_run(OSError(f"spawn failed for {TTY}"))
    with caplog.at_level(logging.INFO, logger="squid_pet.focus"):
        focus._run_osascript(focus.build_terminal_focus_script(TTY))
    (rec,) = caplog.records
    assert rec.levelno == logging.WARNING
    assert "OSError" in rec.getMessage()
    assert TTY not in rec.getMessage()
