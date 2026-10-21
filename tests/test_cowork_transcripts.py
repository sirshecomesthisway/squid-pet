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


def test_cowork_transcript_activity_is_seen(tmp_path):
    f = _cowork_transcript(tmp_path / "cowork")
    now = os.stat(f).st_mtime + 2
    d = _detector(tmp_path, extra_roots_fn=lambda: cowork_projects_roots(
        tmp_path / "cowork"))
    d.is_busy(now=now)
    assert d.transcript_age < 10


def test_injected_projects_dir_ignores_real_cowork_roots(tmp_path):
    d = _detector(tmp_path)
    assert d._extra_roots_fn is None
