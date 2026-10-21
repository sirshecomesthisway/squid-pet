"""The desktop app's Cowork sessions keep transcripts under their own
CLAUDE_CONFIG_DIR, not ~/.claude/projects. All paths are temp dirs."""
from __future__ import annotations

import os

from squid_pet.detectors import ClaudeCodeDetector, cowork_projects_roots


class _FakeProc:
    pid = 1


def _cowork_transcript(base, session="c7981a34", sid="4dd79a75"):
    d = base / "org" / "acct" / session / ".claude" / "projects" / "session"
    d.mkdir(parents=True)
    f = d / f"{sid}.jsonl"
    f.write_text("")
    return f


def test_roots_found_at_expected_depth(tmp_path):
    f = _cowork_transcript(tmp_path)
    assert cowork_projects_roots(tmp_path) == [f.parent.parent]


def test_missing_base_gives_no_roots(tmp_path):
    assert cowork_projects_roots(tmp_path / "nope") == []


def _detector(tmp_path, **kw):
    return ClaudeCodeDetector(
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        shell_cmdline_fn=lambda p: None,
        projects_dir=tmp_path / "home_projects",
        recent_file_ages_fn=lambda: [],
        active_sessions_fn=lambda: set(),
        **kw,
    )


def _extra(tmp_path, name="cowork"):
    return lambda: cowork_projects_roots(tmp_path / name)


def test_cowork_transcript_activity_is_seen(tmp_path):
    f = _cowork_transcript(tmp_path / "cowork")
    now = os.stat(f).st_mtime + 2
    d = _detector(tmp_path, extra_roots_fn=_extra(tmp_path))
    d.is_busy(now=now)
    assert 1.5 < d.transcript_age < 2.5


def test_cowork_subagent_transcript_is_seen(tmp_path):
    f = _cowork_transcript(tmp_path / "cowork")
    sub = f.parent / f.stem / "subagents"
    sub.mkdir(parents=True)
    h = sub / "agent-1.jsonl"
    h.write_text("")
    os.utime(f, (1000, 1000))
    os.utime(h, (2000, 2000))
    d = _detector(tmp_path, extra_roots_fn=_extra(tmp_path))
    d.is_busy(now=2003.0)
    assert 2.5 < d.transcript_age < 3.5     # the helper's, not the parent's


def test_two_roots_both_searched(tmp_path):
    a = _cowork_transcript(tmp_path / "cowork", session="s1", sid="a")
    b = _cowork_transcript(tmp_path / "cowork", session="s2", sid="b")
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    d = _detector(tmp_path, extra_roots_fn=_extra(tmp_path))
    d.is_busy(now=2001.0)
    assert d.transcript_age < 2


def test_failing_root_source_does_not_break_home_path(tmp_path):
    def boom():
        raise PermissionError("nope")
    d = _detector(tmp_path, extra_roots_fn=boom)
    d.is_busy(now=1000.0)           # must not raise
    assert d.claude_code_running is True


def test_new_cowork_session_triggers_early_rediscovery(tmp_path):
    first = _cowork_transcript(tmp_path / "cowork", session="s1", sid="a")
    t0 = os.stat(first).st_mtime
    d = _detector(tmp_path, extra_roots_fn=_extra(tmp_path))
    d.is_busy(now=t0 + 1)
    second = _cowork_transcript(tmp_path / "cowork", session="s2", sid="b")
    os.utime(second, (t0 + 9, t0 + 9))
    # Well inside DISCOVERY_CACHE_SEC, but past the new-root check throttle.
    d.is_busy(now=t0 + 10)
    assert second in d._candidates


def test_bare_detector_reads_no_extra_roots(tmp_path):
    assert _detector(tmp_path)._extra_roots_fn is None
