"""post-e2e-polish 2026-06-27 Fix 1: celebrate_hold_sec config knob tests.

Covers:
  (a) Default 20s baseline on GitDetector
  (b) Config override is read at use site (hot-reloadable: each celebrate
      arm picks up the latest config value, no restart needed)
  (c) GitDetector fires celebrate on a new commit

Pink-2026-08-22: the legacy agent's detector tests were converted to
ClaudeCodeDetector (that detector was removed -- the agent was never actually
installed/run on this machine). ClaudeCodeDetector gained the exact same
busy->idle celebrate-edge mechanism this same day.

Pink-2026-08-27f: ClaudeCodeDetector's busy->idle celebrate-edge
machinery (_was_busy/_celebrate_until, tested below) was removed
entirely -- it fired on any >20s gap with no tool call (an ordinary
mid-task reasoning stretch), not a verified completion, confirmed live
as a false "finished with claude!" bubble. The real signal is now
Claude Code's official Stop hook: see
tests/test_watcher_claude_code_cascade.py (state-machine-level
integration) and tests/test_claude_pet_hook_script.py (the hook script
itself) for the replacement coverage. GitDetector's celebrate_hold_sec
tests are untouched -- its signal (an actual branch-tip sha change)
was never part of this problem.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from squid_pet.detectors import GitDetector


# ── (a) Defaults ────────────────────────────────────────────────────────
def test_git_default_celebrate_hold_is_20s():
    """GitDetector class const = 20.0 (was 4.0 pre-Fix-1)."""
    d = GitDetector(project_dirs=[])
    assert d.CELEBRATE_HOLD_SEC == 20.0


def _repo_with_baseline(tmp_path, fake_now):
    """Repo on main at sha A, detector holding its baseline at fake_now-2."""
    gitdir = tmp_path / "myrepo" / ".git"
    (gitdir / "refs" / "heads").mkdir(parents=True)
    head = gitdir / "HEAD"
    head.write_text("ref: refs/heads/main\n")
    os.utime(head, (fake_now - 100, fake_now - 100))
    _commit(gitdir, "a" * 40, fake_now - 100)
    d = GitDetector(project_dirs=[str(tmp_path)])
    d._discovered_at = 0.0  # force first discovery
    assert not d.is_celebrating(fake_now - 2)  # first sighting: baseline only
    return gitdir, d


def _commit(gitdir, sha, ts):
    ref = gitdir / "refs" / "heads" / "main"
    ref.write_text(sha + "\n")
    os.utime(ref, (ts, ts))


# ── (b) Config-override hot-reload ──────────────────────────────────────
def test_git_celebrate_hold_reads_config_on_arm(tmp_path):
    """GitDetector reads celebrate_hold_sec at the moment celebrate arms."""
    fake_now = 1000.0
    gitdir, d = _repo_with_baseline(tmp_path, fake_now)
    _commit(gitdir, "b" * 40, fake_now - 1)
    with patch("squid_pet.config.get", side_effect=lambda k, default=None:
               12.5 if k == "celebrate_hold_sec" else default):
        # is_celebrating() -> _refresh() -> arms _celebrate_until
        assert d.is_celebrating(fake_now), "should fire celebrate on a new commit"
        # Expected: fake_now + 12.5 = 1012.5
        assert abs(d._celebrate_until - 1012.5) < 0.001, \
            f"expected 1012.5, got {d._celebrate_until}"


# ── (c) GitDetector fires celebrate on a new commit ─────────────────────
def test_git_celebrate_fires_on_new_commit(tmp_path):
    """End-to-end: branch tip moves between scans -> is_celebrating True."""
    fake_now = 1000.0
    gitdir, d = _repo_with_baseline(tmp_path, fake_now)
    _commit(gitdir, "b" * 40, fake_now - 1)
    assert d.is_celebrating(fake_now), \
        "GitDetector should report celebrating when the branch tip changes"
