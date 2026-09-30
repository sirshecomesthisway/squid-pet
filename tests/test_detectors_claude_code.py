"""Tests for ClaudeCodeDetector -- mirrors the legacy detector's removed
style. All psutil / filesystem dependencies are injected so no real
process table or disk is touched."""
from __future__ import annotations

import os
from pathlib import Path

from squid_pet.detectors import ClaudeCodeDetector


class _FakeProc:
    def __init__(self, pid=1234): self.pid = pid


class _FakeStat:
    def __init__(self, mtime): self.st_mtime = mtime


def _make(
    procs=None, cpu=0.0, shell_active=False,
    transcripts=None,  # dict[str path] -> mtime
    enabled=True, projects_dir=None, file_ages=None,
    shell_cmdline=None,  # Pink-2026-08-27k: injected latest_shell_child_cmdline result
):
    transcripts = transcripts or {}
    paths = [Path(p) for p in transcripts]

    def _stat(p):
        if p in transcripts:
            return _FakeStat(transcripts[p])
        raise OSError(f"no such file: {p}")

    return ClaudeCodeDetector(
        enabled=enabled,
        find_processes_fn=lambda: list(procs or []),
        aggregate_cpu_fn=lambda p: cpu,
        has_active_shell_children_fn=lambda p: shell_active,
        shell_cmdline_fn=lambda p: shell_cmdline,
        projects_dir=projects_dir or Path("/fake/.claude/projects"),
        glob_fn=lambda root: iter(paths),
        stat_fn=_stat,
        # Hermetic by default: no real disk walk. Tests exercising
        # file_active pass file_ages explicitly.
        recent_file_ages_fn=lambda: list(file_ages or []),
    )


def test_no_processes_is_quiet():
    d = _make(procs=[])
    assert d.is_busy(now=1000.0) is False
    assert d.is_celebrating(now=1000.0) is False
    assert d.is_grooving(now=1000.0) is False
    assert d.claude_code_running is False


def test_shell_active_fires_busy_immediately():
    """Live tool subprocess (e.g. a Bash-tool call) is busy on tick 1 --
    no streak needed, same as the legacy detector's shell_active."""
    d = _make(procs=[_FakeProc()], cpu=0.5, shell_active=True)
    assert d.is_busy(now=1000.0) is True


# ── shell_cmdline (2026-08-27k: feeds the working reannounce bubble) ───
def test_shell_cmdline_populated_when_shell_active():
    d = _make(procs=[_FakeProc()], shell_active=True, shell_cmdline=["pytest", "-v"])
    d.is_busy(now=1000.0)
    assert d.shell_cmdline == ["pytest", "-v"]


def test_shell_cmdline_none_when_shell_not_active():
    """Even if shell_cmdline_fn would return something, it must not be
    consulted/reported when has_active_shell_children_fn says False --
    avoids reporting a stale/unrelated cmdline."""
    d = _make(procs=[_FakeProc()], shell_active=False, shell_cmdline=["stale", "cmd"])
    d.is_busy(now=1000.0)
    assert d.shell_cmdline is None


def test_shell_cmdline_defaults_to_real_watcher_fn_when_not_injected():
    """Production construction (no shell_cmdline_fn passed) must not
    crash -- falls back to watcher.latest_shell_child_cmdline, which is
    itself best-effort against these fake process objects (no real
    .children()) and returns None rather than raising."""
    d = ClaudeCodeDetector(
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: True,
        projects_dir=Path("/fake/.claude/projects"),
        glob_fn=lambda root: iter([]),
        stat_fn=lambda p: (_ for _ in ()).throw(OSError()),
        recent_file_ages_fn=lambda: [],
    )
    d.is_busy(now=1000.0)  # must not raise
    assert d.shell_cmdline is None


def test_fresh_transcript_fires_streaming_and_busy():
    now = 1000.0
    d = _make(
        procs=[_FakeProc()],
        transcripts={"/fake/.claude/projects/p/s.jsonl": now - 2.0},
    )
    assert d.is_busy(now=now) is True
    assert d.streaming is True
    assert d.transcript_age == 2.0


def test_stale_transcript_is_quiet():
    now = 1000.0
    d = _make(
        procs=[_FakeProc()],
        transcripts={"/fake/.claude/projects/p/s.jsonl": now - 300.0},
    )
    assert d.is_busy(now=now) is False
    assert d.streaming is False


def test_no_transcripts_is_quiet():
    d = _make(procs=[_FakeProc()], transcripts={})
    assert d.is_busy(now=1000.0) is False
    assert d.transcript_age == float("inf")


def test_newest_transcript_wins_across_multiple_sessions():
    now = 1000.0
    d = _make(
        procs=[_FakeProc()],
        transcripts={
            "/fake/.claude/projects/a/old.jsonl": now - 500.0,
            "/fake/.claude/projects/b/new.jsonl": now - 3.0,
        },
    )
    d.is_busy(now=now)
    assert d.transcript_age == 3.0


def test_recent_file_write_fires_busy_and_file_active():
    """Edit/Write-style tool calls never spawn a subprocess, so
    shell_active alone misses them -- this is the signal that catches
    that gap (verified against a real user report, 2026-08-14: Squid
    stayed 'thinking' while Claude was actively editing files)."""
    d = _make(procs=[_FakeProc()], file_ages=[2.0])
    assert d.is_busy(now=1000.0) is True
    assert d.file_active is True


def test_stale_file_write_is_quiet():
    """A file write older than FILE_ACTIVE_WINDOW_SEC doesn't count --
    the age list here is empty because the caller (recent_file_ages_fn)
    is responsible for the window filtering, mirroring IDEDetector."""
    d = _make(procs=[_FakeProc()], file_ages=[])
    assert d.is_busy(now=1000.0) is False
    assert d.file_active is False


def test_file_active_requires_process_running():
    """No claude process -> no file scan attributed to it, even if the
    injected fn would otherwise report a fresh write."""
    d = _make(procs=[], file_ages=[1.0])
    assert d.is_busy(now=1000.0) is False
    assert d.file_active is False


def test_is_celebrating_always_false_regardless_of_busy_drop():
    """Pink-2026-08-27f: the busy->idle celebrate-edge heuristic (shell_active
    dropping) was removed entirely -- it fired on any >20s gap with no tool
    call, a normal mid-task reasoning stretch, confirmed live as a false
    "finished with claude!" bubble while Claude was still working.
    is_celebrating() is now a permanent stub (real signal is Claude Code's
    Stop hook, read directly in watcher.claude_sessions_just_finished() /
    StateMachine._compute_inner() -- see test_watcher_claude_code_cascade.py
    for that integration). This locks in that a busy->idle drop on this
    detector alone can never again produce a celebrate signal."""
    state = {"shell_active": True}
    d = ClaudeCodeDetector(
        enabled=True,
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: state["shell_active"],
        projects_dir=Path("/fake"),
        glob_fn=lambda root: iter([]),
        stat_fn=lambda p: (_ for _ in ()).throw(OSError()),
        recent_file_ages_fn=lambda: [],
    )
    assert d.is_busy(now=1.0) is True
    state["shell_active"] = False
    assert d.is_celebrating(now=2.0) is False
    assert d.is_celebrating(now=2.5) is False
    assert d.is_celebrating(now=30.0) is False


def test_disabled_detector_always_returns_false():
    d = _make(
        procs=[_FakeProc()], shell_active=True,
        transcripts={"/x.jsonl": 999.5}, enabled=False,
    )
    assert d.is_busy(now=1000.0) is False
    assert d.is_celebrating(now=1000.0) is False
    assert d.is_grooving(now=1000.0) is False


# ── subagent transcripts (2026-09-24) ─────────────────────────────────
# A Task-tool helper (foreground or background) writes its OWN transcript
# at <enc>/<session_id>/subagents/agent-<id>.jsonl while the parent's stays
# quiet. The default discovery glob must pick those up so transcript_age /
# streaming reflect helper activity through the existing cascade.
def _write(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    os.utime(path, (mtime, mtime))


def test_default_glob_discovers_subagent_transcripts(tmp_path):
    """No glob_fn injected: the real default glob must find a subagent
    transcript nested under <enc>/<session_id>/subagents/, not just the
    parent-level */*.jsonl file."""

    now = 1000.0
    projects = tmp_path / "projects"
    enc = projects / "-Users-me-proj"
    # quiet parent transcript, fresh subagent transcript
    _write(enc / "sess.jsonl", now - 300.0)
    sub = enc / "sess" / "subagents" / "agent-a0e33328136f521e9.jsonl"
    _write(sub, now - 2.0)
    # a .meta.json sibling must NOT be matched
    meta = enc / "sess" / "subagents" / "agent-a0e33328136f521e9.meta.json"
    meta.write_text("{}")

    d = ClaudeCodeDetector(
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=projects,
        stat_fn=os.stat,
        recent_file_ages_fn=lambda: [],
    )
    assert d.is_busy(now=now) is True
    assert d.streaming is True
    assert d.transcript_age == 2.0
    # the freshest candidate (the subagent file) is what she locked onto
    assert d.transcript_path is not None
    assert d.transcript_path.endswith(
        "sess/subagents/agent-a0e33328136f521e9.jsonl")


def test_meta_json_sibling_is_not_discovered(tmp_path):
    """Only agent-*.jsonl subagent transcripts count -- never the
    agent-<id>.meta.json sidecar."""

    now = 1000.0
    projects = tmp_path / "projects"
    sub = projects / "-Users-me-proj" / "sess" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-abc.meta.json").write_text("{}")
    os.utime(sub / "agent-abc.meta.json", (now - 1.0, now - 1.0))

    d = ClaudeCodeDetector(
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=projects,
        stat_fn=os.stat,
        recent_file_ages_fn=lambda: [],
    )
    assert d.is_busy(now=now) is False
    assert d.transcript_age == float("inf")


def test_subagent_transcript_activity_tail_is_read(tmp_path):
    """The bounded-tail ledger logic must apply to subagent files too: a
    fresh mtime whose only tail record is a background artifact ledger line
    contributes NO activity (same content-blind rule as the parent)."""

    now = 1000.0
    projects = tmp_path / "projects"
    sub = projects / "-Users-me-proj" / "sess" / "subagents" / "agent-x.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text('{"type": "artifact-autoreact-ledger"}\n')
    os.utime(sub, (now - 2.0, now - 2.0))

    d = ClaudeCodeDetector(
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=projects,
        stat_fn=os.stat,
        recent_file_ages_fn=lambda: [],
    )
    # mtime is fresh (2s) but the only record is a ledger line -> no activity
    assert d.is_busy(now=now) is False
    assert d.streaming is False


def test_diagnostic_contains_required_keys():
    d = _make(procs=[_FakeProc()], cpu=12.5)
    d.is_busy(now=1000.0)
    diag = d.diagnostic()
    for key in ("name", "enabled", "claude_code_running", "cpu_percent",
                "shell_active", "file_active", "transcript_age", "streaming"):
        assert key in diag, f"missing {key}"
    assert diag["name"] == "claude_code"


def test_scan_cache_dedupes_within_same_tick():
    """Calling is_busy with the same `now` twice should scan once."""
    calls = {"n": 0}
    procs = [_FakeProc()]

    def find():
        calls["n"] += 1
        return procs

    d = ClaudeCodeDetector(
        enabled=True,
        find_processes_fn=find,
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=Path("/fake"),
        glob_fn=lambda root: iter([]),
        stat_fn=lambda p: (_ for _ in ()).throw(OSError()),
        recent_file_ages_fn=lambda: [],
    )
    d.is_busy(now=5.0)
    d.is_busy(now=5.0)
    assert calls["n"] == 1
    d.is_busy(now=6.0)
    assert calls["n"] == 2


def test_discovery_cache_reused_within_window():
    """The candidate-file glob should only be re-run once per
    DISCOVERY_CACHE_SEC, mirroring GitDetector's repo-discovery cache."""
    calls = {"n": 0}

    def glob_fn(root):
        calls["n"] += 1
        return iter([Path("/fake/.claude/projects/p/s.jsonl")])

    d = ClaudeCodeDetector(
        enabled=True,
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=Path("/fake/.claude/projects"),
        glob_fn=glob_fn,
        stat_fn=lambda p: _FakeStat(995.0),
        recent_file_ages_fn=lambda: [],
        # Hermetic: no active-turn flag read from the real ~/.squid-pet (a live
        # turn whose session isn't in this fake candidate set would otherwise
        # force a rediscovery -- finding B). This test pins the cache window.
        active_sessions_fn=lambda: set(),
    )
    d.is_busy(now=1000.0)
    d.is_busy(now=1010.0)
    assert calls["n"] == 1  # still within 60s cache window
    d.is_busy(now=1000.0 + ClaudeCodeDetector.DISCOVERY_CACHE_SEC + 1)
    assert calls["n"] == 2  # cache expired, re-globbed


# ── CPU fix 2 (2026-09-03): one child-tree walk per tick ──────────────
class _WalkCountingProc:
    """A process whose descendant tree we can count traversals of."""

    def __init__(self, children):
        self.pid = 4321
        self._children = children
        self.walks = 0

    def children(self, recursive=False):
        self.walks += 1
        return self._children


class _WalkChild:
    def __init__(self, name, cmdline):
        self._name, self._cmdline = name, cmdline

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline


def _walk_counting_detector(proc):
    """Production shell wiring: neither shell seam injected, so the
    detector must reach for watcher.shell_child_activity itself."""
    return ClaudeCodeDetector(
        find_processes_fn=lambda: [proc],
        aggregate_cpu_fn=lambda p: 0.0,
        projects_dir=Path("/fake/.claude/projects"),
        glob_fn=lambda root: iter([]),
        stat_fn=lambda p: (_ for _ in ()).throw(OSError()),
        recent_file_ages_fn=lambda: [],
    )


def test_uninjected_shell_signals_walk_the_child_tree_once_per_tick():
    """Both signals came from one walk, not two: has_active_shell_children
    and latest_shell_child_cmdline used to traverse the same tree back to
    back every tick, for both agents."""
    proc = _WalkCountingProc([_WalkChild("pytest", ["pytest", "-v"])])
    d = _walk_counting_detector(proc)

    d.is_busy(now=1000.0)

    assert proc.walks == 1
    assert d.shell_active is True
    assert d.shell_cmdline == ["pytest", "-v"]


def test_uninjected_wrapper_only_child_reports_the_wrapper_cmdline():
    """The wrapper shell counts as tool activity (unchanged), and its
    cmdline is now the reportable fallback: when no real tool grandchild is
    caught, the wrapper carries the command inside its `eval '<cmd>'`, which
    the observer recovers. (Previously shell_cmdline was None here.)"""
    args = ["/bin/zsh", "-c", "source snap.sh && eval 'pytest -v' < /dev/null"]
    proc = _WalkCountingProc([_WalkChild("zsh", args)])
    d = _walk_counting_detector(proc)

    d.is_busy(now=1000.0)

    assert proc.walks == 1
    assert d.shell_active is True
    assert d.shell_cmdline == args


# ── finding 3: a subagent created mid-cache is seen on the next tick ────
# _discover caches candidates for DISCOVERY_CACHE_SEC (60s). A subagent
# transcript created AFTER the last full discovery must not stay invisible for
# up to 60s -- after the parent's Stop + 20s groove beat that would read as idle
# for ~40s while a helper works. A new file bumps its subagents/ dir mtime, so a
# cheap per-tick stat of the cached parents' subagents/ dirs surfaces it fast.
def test_new_subagent_seen_before_discovery_cache_expires(tmp_path):
    now = 1000.0
    projects = tmp_path / "projects"
    enc = projects / "-Users-me-proj"
    # A parent transcript exists at first discovery: old enough that it alone is
    # NOT streaming (100s > STREAMING_STALE_SEC) but young enough to be a cached
    # candidate (< CANDIDATE_MAX_AGE_SEC). This is the post-Stop state.
    _write(enc / "sess.jsonl", now - 100.0)

    d = ClaudeCodeDetector(
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=projects,
        stat_fn=os.stat,
        recent_file_ages_fn=lambda: [],
        active_sessions_fn=lambda: set(),
    )
    # First tick: full discovery, parent alone -> not streaming -> idle.
    assert d.is_busy(now=now) is False
    assert d.streaming is False

    # A helper starts writing 30s later -- still inside the 60s discovery cache.
    later = now + 30.0
    sub = enc / "sess" / "subagents" / "agent-a0e3.jsonl"
    _write(sub, later - 2.0)
    # .meta.json sidecar must still be excluded even on the incremental path.
    (sub.parent / "agent-a0e3.meta.json").write_text("{}")

    # Next tick, WITHOUT a full rediscovery (30s < 60s): the new subagent must
    # already be visible.
    assert d.is_busy(now=later) is True
    assert d.streaming is True
    assert d.transcript_path is not None
    assert d.transcript_path.endswith("sess/subagents/agent-a0e3.jsonl")


def test_incremental_subagent_scan_ignores_meta_sidecar(tmp_path):
    """On the incremental (within-cache) path, a NEW .meta.json sidecar
    appearing in a cached parent's subagents/ dir must not be discovered."""
    now = 1000.0
    projects = tmp_path / "projects"
    enc = projects / "-Users-me-proj"
    _write(enc / "sess.jsonl", now - 100.0)

    d = ClaudeCodeDetector(
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=projects,
        stat_fn=os.stat,
        recent_file_ages_fn=lambda: [],
        active_sessions_fn=lambda: set(),
    )
    assert d.is_busy(now=now) is False

    later = now + 30.0
    meta = enc / "sess" / "subagents" / "agent-a0e3.meta.json"
    _write(meta, later - 1.0)

    assert d.is_busy(now=later) is False
    assert d.streaming is False


# ── finding B: a resumed stale parent's helper is seen via the active flag ─
# The incremental top-up only watches subagents/ dirs of parents ALREADY in
# _candidates. If parent A is >15min old (not a candidate) while unrelated B
# keeps the cache warm, A's newly launched helper -- and A's first helper also
# creates the subagents/ dir itself -- would stay invisible until the next full
# rediscovery (<=60s). A live claude_turn_active/<sid> flag naming a session not
# in the candidates forces one full rediscovery so A + its helper are seen now.
def test_resumed_stale_parent_helper_seen_via_active_flag(tmp_path):
    now = 1000.0
    projects = tmp_path / "projects"
    enc_a = projects / "-Users-me-projA"
    enc_b = projects / "-Users-me-projB"
    _write(enc_b / "sessB.jsonl", now - 5.0)          # B fresh -> keeps cache warm
    _write(enc_a / "sessA.jsonl", now - 2000.0)       # A stale (> CANDIDATE_MAX_AGE)

    active = {"ids": set()}
    d = ClaudeCodeDetector(
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=projects,
        stat_fn=os.stat,
        recent_file_ages_fn=lambda: [],
        active_sessions_fn=lambda: set(active["ids"]),
    )
    # First tick: full discovery. Only B is a candidate (A too old).
    assert d.is_busy(now=now) is True
    assert d.streaming is True

    # 30s later (still inside the 60s cache): B has gone quiet, the user resumed
    # A (turn opens -> active flag names sessA) and A launched its FIRST helper,
    # creating sessA/subagents/ and a fresh transcript in it.
    later = now + 30.0
    _write(enc_b / "sessB.jsonl", later - 500.0)      # B quiet now
    sub_a = enc_a / "sessA" / "subagents" / "agent-a1.jsonl"
    _write(sub_a, later - 2.0)
    active["ids"] = {"sessA"}

    assert d.is_busy(now=later) is True
    assert d.streaming is True
    assert d.transcript_path is not None
    assert d.transcript_path.endswith("sessA/subagents/agent-a1.jsonl")


def test_no_active_flag_leaves_stale_parent_helper_until_rediscovery(tmp_path):
    """Control: with NO active flag naming the stale parent, its helper stays
    invisible until the next full rediscovery -- the bounded <=60s limitation
    the trigger above closes only when a live turn flag is present."""
    now = 1000.0
    projects = tmp_path / "projects"
    enc_a = projects / "-Users-me-projA"
    enc_b = projects / "-Users-me-projB"
    _write(enc_b / "sessB.jsonl", now - 5.0)
    _write(enc_a / "sessA.jsonl", now - 2000.0)

    d = ClaudeCodeDetector(
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=projects,
        stat_fn=os.stat,
        recent_file_ages_fn=lambda: [],
        active_sessions_fn=lambda: set(),
    )
    assert d.is_busy(now=now) is True

    later = now + 30.0
    _write(enc_b / "sessB.jsonl", later - 500.0)
    _write(enc_a / "sessA" / "subagents" / "agent-a1.jsonl", later - 2.0)

    # No active flag -> no forced rediscovery -> A's helper not yet seen.
    assert d.is_busy(now=later) is False


def test_stale_active_flag_with_no_transcript_does_not_reglob_every_tick(tmp_path):
    """A persistently-flagged session with no on-disk transcript (e.g. a stale
    turn_active flag) must force at most ONE full rediscovery, never a glob
    storm every tick."""
    now = 1000.0
    projects = tmp_path / "projects"
    _write(projects / "-Users-me-projB" / "sessB.jsonl", now - 5.0)

    globs = {"n": 0}

    def _counting_glob(root):
        globs["n"] += 1
        from squid_pet.detectors import _default_claude_transcript_glob
        return _default_claude_transcript_glob(root)

    d = ClaudeCodeDetector(
        find_processes_fn=lambda: [_FakeProc()],
        aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        projects_dir=projects,
        glob_fn=_counting_glob,
        stat_fn=os.stat,
        recent_file_ages_fn=lambda: [],
        active_sessions_fn=lambda: {"ghost-session"},  # never on disk
    )
    d.is_busy(now=now)              # 1st full glob
    assert globs["n"] == 1
    d.is_busy(now=now + 5.0)        # forces ONE rediscovery for the ghost
    assert globs["n"] == 2
    d.is_busy(now=now + 10.0)       # ghost already attempted -> no re-glob
    d.is_busy(now=now + 15.0)
    assert globs["n"] == 2
