"""Phase A -- which surface each Claude Code session runs on.

Claude Code reaches this machine on several surfaces, and squid used to treat
them all as one. The hook now reads the TOP-LEVEL ``entrypoint`` field of the
session's own transcript (``"cli"`` for the terminal, ``"local-agent"`` for the
desktop app's Code tab, ``"sdk-cli"`` for ``claude -p``) and records it in
``claude_session_surface/<session_id>``; the watcher lists that dir, exposes it
on the snapshot / state.json, and ``squid why`` prints it.

Privacy pins: only the top-level ``entrypoint`` of a record is ever used --
never message text or content blocks -- and the read is bounded.
Hermetic: every path is under tmp_path; nothing reads the real ~/.claude or
~/.squid-pet.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.test_claude_pet_hook_script import _load_hook_module, _run


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "squid-pet-home"


def _surface_path(home: Path, sid: str) -> Path:
    return home / "claude_session_surface" / sid


def _record(entrypoint: str | None = None, rtype: str = "assistant", **extra) -> str:
    rec: dict = {"type": rtype, "sessionId": "s", **extra}
    if entrypoint is not None:
        rec["entrypoint"] = entrypoint
    return json.dumps(rec, separators=(",", ":"))


def _transcript(tmp_path: Path, lines: list[str], name: str = "t.jsonl") -> Path:
    p = tmp_path / "projects" / "-enc" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(line + "\n" for line in lines))
    return p


def _event(sid: str, event: str, transcript: Path | str | None, **extra) -> dict:
    payload = {"session_id": sid, "hook_event_name": event, **extra}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    return payload


# ── hook: resolving and recording the surface ─────────────────────────────
@pytest.mark.parametrize("entrypoint", ["cli", "local-agent", "sdk-cli"])
def test_hook_records_the_known_surface_from_the_transcript(home, tmp_path, entrypoint):
    t = _transcript(tmp_path, [_record(entrypoint, "user"), _record(entrypoint)])
    r = _run(_event("sess-1", "UserPromptSubmit", t), home)
    assert r.returncode == 0 and r.stderr == ""
    assert _surface_path(home, "sess-1").read_text() == entrypoint


def test_an_unrecognised_entrypoint_degrades_to_unknown(home, tmp_path):
    t = _transcript(tmp_path, [_record("claude-vscode")])
    r = _run(_event("sess-1", "Stop", t), home)
    assert r.returncode == 0 and r.stderr == ""
    assert _surface_path(home, "sess-1").read_text() == "unknown"


def test_a_non_string_entrypoint_degrades_to_unknown(home, tmp_path):
    t = _transcript(tmp_path, [json.dumps({"type": "user", "entrypoint": ["x"]})])
    _run(_event("sess-1", "Stop", t), home)
    assert _surface_path(home, "sess-1").read_text() == "unknown"


def test_the_newest_record_wins_after_a_cross_surface_resume(home, tmp_path):
    """A desktop session resumed in a terminal (or vice versa) writes the new
    surface on its newer records -- the tail decides."""
    t = _transcript(tmp_path, [_record("local-agent"), _record("local-agent"),
                               _record("cli", "user"), _record("cli")])
    _run(_event("sess-1", "UserPromptSubmit", t), home)
    assert _surface_path(home, "sess-1").read_text() == "cli"


def test_trailing_bookkeeping_records_without_an_entrypoint_are_skipped(home, tmp_path):
    """Real transcripts end with ai-title / mode / last-prompt records that
    carry no entrypoint; the newest record that HAS one decides."""
    t = _transcript(tmp_path, [_record("local-agent"),
                               _record(None, "ai-title"), _record(None, "mode"),
                               _record(None, "last-prompt")])
    _run(_event("sess-1", "Stop", t), home)
    assert _surface_path(home, "sess-1").read_text() == "local-agent"


def test_only_the_top_level_entrypoint_is_used_never_content(home, tmp_path):
    """Privacy + correctness: an `entrypoint` key nested in a tool input, or the
    literal text inside a message, must never decide the surface."""
    nested = json.dumps({
        "type": "assistant", "entrypoint": "cli",
        "message": {"content": [{"type": "tool_use",
                                 "input": {"entrypoint": "local-agent"}},
                                {"type": "text",
                                 "text": '"entrypoint":"local-agent"'}]},
    })
    nested_only = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use",
                                 "input": {"entrypoint": "local-agent"}}]},
    })
    t = _transcript(tmp_path, [nested, nested_only])
    _run(_event("sess-1", "Stop", t), home)
    assert _surface_path(home, "sess-1").read_text() == "cli"


@pytest.mark.parametrize("transcript", [None, "", "relative.jsonl", 42,
                                        "/nonexistent/dir/x.jsonl"])
def test_missing_or_bad_transcript_path_writes_nothing_and_never_raises(home, transcript):
    payload = {"session_id": "sess-1", "hook_event_name": "Stop"}
    if transcript is not None:
        payload["transcript_path"] = transcript
    r = _run(payload, home)
    assert r.returncode == 0 and r.stderr == ""
    assert not _surface_path(home, "sess-1").exists()
    # The rest of the hook still ran.
    assert (home / "claude_finished" / "sess-1").exists()


def test_a_transcript_with_no_entrypoint_anywhere_writes_nothing(home, tmp_path):
    """Undeterminable is not "unknown": nothing is written, so a later event
    (or an older value already on disk) still decides."""
    t = _transcript(tmp_path, [_record(None, "mode"), "not json at all", "{"])
    r = _run(_event("sess-1", "Stop", t), home)
    assert r.returncode == 0 and r.stderr == ""
    assert not _surface_path(home, "sess-1").exists()


def test_an_undeterminable_read_keeps_the_existing_entry(home, tmp_path):
    d = home / "claude_session_surface"
    d.mkdir(parents=True)
    _surface_path(home, "sess-1").write_text("local-agent")
    t = _transcript(tmp_path, [_record(None, "mode")])
    _run(_event("sess-1", "PostToolUse", t), home)
    assert _surface_path(home, "sess-1").read_text() == "local-agent"


def test_a_directory_or_fifo_transcript_path_is_refused_without_blocking(home, tmp_path):
    """A FIFO would block a plain open(); the hook blocks the user's turn, so it
    must refuse anything that is not a regular file -- promptly."""
    fifo = tmp_path / "pipe.jsonl"
    os.mkfifo(fifo)
    started = time.monotonic()
    r = _run(_event("sess-1", "Stop", fifo), home)
    assert time.monotonic() - started < 8
    assert r.returncode == 0 and r.stderr == ""
    assert not _surface_path(home, "sess-1").exists()
    d = tmp_path / "dir.jsonl"
    d.mkdir()
    r = _run(_event("sess-2", "Stop", d), home)
    assert r.returncode == 0 and not _surface_path(home, "sess-2").exists()


def test_an_unchanged_surface_is_touched_not_rewritten(home, tmp_path):
    """Keeps a long-lived session's entry clear of the watcher's 2h stale sweep
    (same contract as the tty registry) without churning the file."""
    d = home / "claude_session_surface"
    d.mkdir(parents=True)
    p = _surface_path(home, "sess-1")
    p.write_text("local-agent")
    ino = p.stat().st_ino
    os.utime(p, (1000, 1000))
    t = _transcript(tmp_path, [_record("local-agent")])
    _run(_event("sess-1", "PostToolUse", t), home)
    assert p.read_text() == "local-agent"
    assert p.stat().st_ino == ino, "unchanged surface was rewritten, not touched"
    assert time.time() - p.stat().st_mtime < 60


def test_a_changed_surface_is_rewritten(home, tmp_path):
    d = home / "claude_session_surface"
    d.mkdir(parents=True)
    _surface_path(home, "sess-1").write_text("local-agent")
    t = _transcript(tmp_path, [_record("cli")])
    _run(_event("sess-1", "UserPromptSubmit", t), home)
    assert _surface_path(home, "sess-1").read_text() == "cli"


def test_session_end_removes_the_surface_entry(home, tmp_path):
    d = home / "claude_session_surface"
    d.mkdir(parents=True)
    _surface_path(home, "sess-1").write_text("local-agent")
    t = _transcript(tmp_path, [_record("local-agent")])
    r = _run(_event("sess-1", "SessionEnd", t), home)
    assert r.returncode == 0
    assert not _surface_path(home, "sess-1").exists()


def test_helper_events_never_resolve_but_keep_the_entry_fresh(home, tmp_path):
    """A subagent shares its parent's session id: it may not write the parent's
    surface (its transcript_path is not the parent's), only keep it alive."""
    d = home / "claude_session_surface"
    d.mkdir(parents=True)
    p = _surface_path(home, "sess-1")
    p.write_text("local-agent")
    os.utime(p, (1000, 1000))
    t = _transcript(tmp_path, [_record("cli")])
    _run(_event("sess-1", "PostToolUse", t, agent_id="abc123"), home)
    assert p.read_text() == "local-agent"
    assert time.time() - p.stat().st_mtime < 60
    # ...and creates nothing for a session that has no entry.
    _run(_event("sess-2", "PostToolUse", t, agent_id="abc123"), home)
    assert not _surface_path(home, "sess-2").exists()


def test_the_surface_tmp_never_looks_like_a_session(home, tmp_path):
    """The atomic publish leaves only the session-named file -- no dotfile tmp
    survives, and no non-dot stray the watcher would list as a session."""
    t = _transcript(tmp_path, [_record("local-agent")])
    _run(_event("sess-1", "Stop", t), home)
    assert sorted(os.listdir(home / "claude_session_surface")) == ["sess-1"]


def test_the_read_is_bounded_even_for_a_huge_transcript(tmp_path, monkeypatch):
    """Hooks block the turn: never slurp a multi-MB transcript. The newest
    entrypoint lives in the tail, so only a bounded window is read."""
    mod = _load_hook_module(tmp_path)
    big = "x" * 2000
    lines = [_record("local-agent", content=big) for _ in range(3000)]  # ~6 MB
    lines.append(_record("cli"))
    t = _transcript(tmp_path, lines)
    assert t.stat().st_size > 5_000_000
    read_sizes: list[int] = []
    real_pread = os.pread

    def counting_pread(fd, n, offset):
        read_sizes.append(n)
        return real_pread(fd, n, offset)

    monkeypatch.setattr(os, "pread", counting_pread)
    assert mod._transcript_surface(str(t), use_head=True) == "cli"
    assert sum(read_sizes) <= 2 * mod._SURFACE_READ_BYTES


def test_head_fallback_when_the_tail_has_no_entrypoint(tmp_path):
    """A single record bigger than the tail window (e.g. a huge Write tool call)
    hides every entrypoint from the tail; with no entry on disk yet, the head
    of the transcript (the session's first records) is the bounded fallback."""
    mod = _load_hook_module(tmp_path)
    huge = json.dumps({"type": "assistant", "entrypoint": "cli",
                       "pad": "y" * (mod._SURFACE_READ_BYTES * 2)})
    t = _transcript(tmp_path, [_record("local-agent", "user"), huge])
    assert mod._transcript_surface(str(t), use_head=False) is None
    assert mod._transcript_surface(str(t), use_head=True) == "local-agent"


def test_surface_recording_never_raises(tmp_path, monkeypatch):
    mod = _load_hook_module(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(mod, "_transcript_surface", boom)
    mod._record_session_surface("sess-1", "Stop", str(tmp_path / "x.jsonl"),
                                is_helper=False)


# ── watcher: listing, normalising, sweeping ───────────────────────────────
@pytest.fixture
def surface_dir(tmp_path, monkeypatch):
    from squid_pet import watcher
    d = tmp_path / "claude_session_surface"
    d.mkdir()
    monkeypatch.setattr(watcher, "CLAUDE_SESSION_SURFACE_DIR", str(d))
    monkeypatch.setattr(watcher, "_CLAUDE_SURFACE_CACHE", {})
    return d


def test_watcher_lists_every_session_surface(surface_dir):
    from squid_pet import watcher
    (surface_dir / "a").write_text("cli")
    (surface_dir / "b").write_text("local-agent\n")
    (surface_dir / "c").write_text("sdk-cli")
    (surface_dir / ".b.123.tmp").write_text("cli")  # in-flight tmp: never a session
    assert watcher.claude_session_surfaces() == {
        "a": "cli", "b": "local-agent", "c": "sdk-cli"}


def test_watcher_normalises_unexpected_content_to_unknown(surface_dir):
    from squid_pet import watcher
    (surface_dir / "a").write_text("<script>")
    (surface_dir / "b").write_text("")
    (surface_dir / "c").write_text("unknown")
    assert watcher.claude_session_surfaces() == {
        "a": "unknown", "b": "unknown", "c": "unknown"}


def test_watcher_missing_dir_is_empty(tmp_path, monkeypatch):
    from squid_pet import watcher
    monkeypatch.setattr(watcher, "CLAUDE_SESSION_SURFACE_DIR", str(tmp_path / "nope"))
    assert watcher.claude_session_surfaces() == {}
    assert watcher.claude_session_surface("x") is None


def test_watcher_sweeps_entries_past_the_crash_safety_window(surface_dir):
    """A session that died without SessionEnd must not leak its entry: same 2h
    crash-safety sweep as the sibling flag dirs."""
    from squid_pet import watcher
    old = surface_dir / "dead"
    old.write_text("cli")
    stale = time.time() - watcher.CLAUDE_SESSION_SURFACE_STALE_SEC - 10
    os.utime(old, (stale, stale))
    (surface_dir / "live").write_text("local-agent")
    assert watcher.claude_session_surfaces() == {"live": "local-agent"}
    assert not old.exists()


def test_watcher_read_only_listing_does_not_sweep(surface_dir):
    from squid_pet import watcher
    old = surface_dir / "dead"
    old.write_text("cli")
    stale = time.time() - watcher.CLAUDE_SESSION_SURFACE_STALE_SEC - 10
    os.utime(old, (stale, stale))
    assert watcher.claude_session_surfaces(sweep=False) == {}
    assert old.exists()


def test_watcher_picks_up_a_rewrite(surface_dir):
    from squid_pet import watcher
    p = surface_dir / "a"
    p.write_text("local-agent")
    os.utime(p, (time.time() - 5, time.time() - 5))
    assert watcher.claude_session_surfaces() == {"a": "local-agent"}
    p.write_text("cli")
    os.utime(p, None)
    assert watcher.claude_session_surfaces() == {"a": "cli"}


def test_watcher_cache_forgets_vanished_sessions(surface_dir):
    from squid_pet import watcher
    (surface_dir / "a").write_text("cli")
    watcher.claude_session_surfaces()
    (surface_dir / "a").unlink()
    assert watcher.claude_session_surfaces() == {}
    assert watcher._CLAUDE_SURFACE_CACHE == {}


def test_watcher_exact_lookup(surface_dir):
    from squid_pet import watcher
    (surface_dir / "a").write_text("local-agent")
    (surface_dir / "b").write_text("garbage")
    assert watcher.claude_session_surface("a") == "local-agent"
    assert watcher.claude_session_surface("b") == "unknown"
    assert watcher.claude_session_surface("missing") is None
    assert watcher.claude_session_surface("") is None
    assert watcher.claude_session_surface("../escape") is None


def test_compute_exposes_surfaces_on_the_snapshot_and_state_json(
        surface_dir, tmp_path, monkeypatch):
    from dataclasses import asdict

    from squid_pet import watcher
    (surface_dir / "sess-d").write_text("local-agent")
    (surface_dir / "sess-t").write_text("cli")
    monkeypatch.setattr(watcher, "macos_idle_seconds", lambda: 0.0)
    for attr in ("CLAUDE_AWAITING_INPUT_DIR", "CLAUDE_FINISHED_DIR",
                 "CLAUDE_RECAPPING_DIR", "CLAUDE_TASK_COMPLETE_DIR",
                 "CLAUDE_TURN_ACTIVE_DIR"):
        monkeypatch.setattr(watcher, attr, str(tmp_path / "isolated" / attr))
    sm = watcher.StateMachine(detectors=[])
    st = sm.compute(notify=False)
    assert st.claude_surfaces == {"sess-d": "local-agent", "sess-t": "cli"}
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(watcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watcher, "STATE_FILE", state_file)
    watcher.write_state(st)
    assert json.loads(state_file.read_text())["claude_surfaces"] == asdict(st)[
        "claude_surfaces"]


def test_pet_state_defaults_to_no_surfaces():
    from squid_pet import watcher
    a, b = watcher.PetState(), watcher.PetState()
    assert a.claude_surfaces == {}
    a.claude_surfaces["x"] = "cli"
    assert b.claude_surfaces == {}, "mutable default shared between snapshots"


# ── squid why: ClaudeCodeDetector.diagnostic() ────────────────────────────
def _detector(surfaces):
    from squid_pet.detectors import ClaudeCodeDetector
    return ClaudeCodeDetector(
        find_processes_fn=lambda: [], aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=Path("/nonexistent-projects"), glob_fn=lambda root: [],
        recent_file_ages_fn=lambda: [], active_sessions_fn=lambda: set(),
        surfaces_fn=lambda: dict(surfaces))


def test_diagnostic_names_the_working_sessions_surface():
    d = _detector({"sess-d": "local-agent", "sess-t": "cli"})
    d.transcript_path = "/x/projects/-enc/sess-d.jsonl"
    diag = d.diagnostic()
    assert diag["surface"] == "local-agent"
    assert diag["surfaces"] == {"sess-d": "local-agent", "sess-t": "cli"}


def test_diagnostic_surface_follows_a_helper_to_its_parent_session():
    d = _detector({"sess-t": "cli"})
    d.transcript_path = "/x/projects/-enc/sess-t/subagents/agent-1.jsonl"
    assert d.diagnostic()["surface"] == "cli"


def test_diagnostic_surface_is_none_when_unknown_or_idle():
    d = _detector({})
    assert d.diagnostic()["surface"] is None
    d.transcript_path = "/x/projects/-enc/sess-x.jsonl"
    assert d.diagnostic()["surface"] is None


def test_diagnostic_survives_a_broken_surface_source():
    from squid_pet.detectors import ClaudeCodeDetector

    def boom():
        raise OSError("gone")

    d = ClaudeCodeDetector(surfaces_fn=boom)
    diag = d.diagnostic()
    assert diag["surfaces"] == {} and diag["surface"] is None


# ── review fix 3: read-only compute (squid why) must not sweep ────────────
def _stale_entry(surface_dir):
    from squid_pet import watcher
    p = surface_dir / "dead"
    p.write_text("cli")
    stale = time.time() - watcher.CLAUDE_SESSION_SURFACE_STALE_SEC - 10
    os.utime(p, (stale, stale))
    return p


def _bare_machine(tmp_path, monkeypatch):
    from squid_pet import watcher
    monkeypatch.setattr(watcher, "macos_idle_seconds", lambda: 0.0)
    for attr in ("CLAUDE_AWAITING_INPUT_DIR", "CLAUDE_FINISHED_DIR",
                 "CLAUDE_RECAPPING_DIR", "CLAUDE_TASK_COMPLETE_DIR",
                 "CLAUDE_TURN_ACTIVE_DIR"):
        monkeypatch.setattr(watcher, attr, str(tmp_path / "isolated" / attr))
    return watcher.StateMachine(detectors=[])


def test_read_only_compute_leaves_a_stale_surface_entry_on_disk(
        surface_dir, tmp_path, monkeypatch):
    stale = _stale_entry(surface_dir)
    sm = _bare_machine(tmp_path, monkeypatch)
    st = sm.compute(notify=False, sweep_surfaces=False)
    assert stale.exists(), "a read-only diagnostic compute deleted a file"
    assert "dead" not in st.claude_surfaces, "a stale entry is still not reported"


def test_the_daemon_tick_still_sweeps_the_stale_surface_entry(
        surface_dir, tmp_path, monkeypatch):
    stale = _stale_entry(surface_dir)
    sm = _bare_machine(tmp_path, monkeypatch)
    sm.compute()
    assert not stale.exists()


def test_squid_why_does_not_sweep_surface_entries(tmp_path):
    """End to end through the real `--why-json` entry point, with HOME pointed
    at tmp so every ~/.squid-pet and ~/.claude read is isolated."""
    import subprocess
    import sys
    home = tmp_path / "home"
    d = home / ".squid-pet" / "claude_session_surface"
    d.mkdir(parents=True)
    p = d / "dead"
    p.write_text("cli")
    os.utime(p, (1000, 1000))  # far past the 2h sweep window
    env = dict(os.environ, HOME=str(home), SQUID_PET_HOME=str(home / ".squid-pet"))
    r = subprocess.run(
        [sys.executable, "-c",
         "from squid_pet import watcher; "
         "watcher.macos_idle_seconds = lambda: 0.0; "
         "import runpy; runpy.run_module('squid_pet', run_name='__main__')",
         "--why-json"],
        capture_output=True, text=True, timeout=30,
        cwd=str(Path(__file__).parent.parent), env=env)
    assert r.returncode == 0, r.stderr
    assert p.exists(), "`squid why` deleted a surface entry"
