"""Tests for GitDetector using a real tmp_path filesystem.

We create fake .git/HEAD + .git/index files and tweak their mtimes
via os.utime() to simulate commits / staging. Celebration needs a ref sha change.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from squid_pet.detectors import GitDetector


def _make_repo(root: Path, name: str) -> Path:
    """Create root/name/.git/{HEAD,index,refs/heads/main}."""
    repo = root / name
    git = repo / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "index").write_bytes(b"")
    (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    return git


def _touch(p: Path, ts: float) -> None:
    p.touch(exist_ok=True)
    os.utime(str(p), (ts, ts))


SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def _set_ref(git: Path, ref: str, sha: str, ts: float) -> None:
    path = git / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n")
    os.utime(str(path), (ts, ts))


def _set_head(git: Path, text: str, ts: float) -> None:
    (git / "HEAD").write_text(text)
    os.utime(str(git / "HEAD"), (ts, ts))


def _based(tmp_path: Path, now: float) -> tuple[Path, GitDetector]:
    """Repo on main at SHA_A, detector already holding its baseline."""
    git = _make_repo(tmp_path, "myrepo")
    _set_ref(git, "refs/heads/main", SHA_A, now - 100.0)
    _touch(git / "HEAD", now - 100.0)
    d = GitDetector(project_dirs=[str(tmp_path)])
    assert d.is_celebrating(now=now - 50.0) is False
    return git, d


def test_first_scan_is_baseline_only(tmp_path):
    git = _make_repo(tmp_path, "myrepo")
    now = time.time()
    _set_ref(git, "refs/heads/main", SHA_A, now - 1.0)
    _touch(git / "HEAD", now - 1.0)
    d = GitDetector(project_dirs=[str(tmp_path)])
    assert d.is_celebrating(now=now) is False
    assert d.is_celebrating(now=now + 1.0) is False


def test_new_sha_on_same_branch_celebrates(tmp_path):
    now = time.time()
    git, d = _based(tmp_path, now)
    _set_ref(git, "refs/heads/main", SHA_B, now - 1.0)
    assert d.is_celebrating(now=now) is True
    assert "main" not in d._last_celebrate_reason  # no branch names kept
    assert d.is_busy(now=now) is False


def test_head_touch_without_sha_change_does_not_celebrate(tmp_path):
    now = time.time()
    git, d = _based(tmp_path, now)
    _touch(git / "HEAD", now - 1.0)
    assert d.is_celebrating(now=now) is False


def test_switching_branch_does_not_celebrate_even_when_tips_differ(tmp_path):
    now = time.time()
    git, d = _based(tmp_path, now)
    _set_ref(git, "refs/heads/other", SHA_B, now - 90.0)
    _set_head(git, "ref: refs/heads/other\n", now - 1.0)
    assert d.is_celebrating(now=now) is False
    # The switch only re-baselined: a commit on the new branch now fires.
    _set_ref(git, "refs/heads/other", SHA_C, now + 1.0)
    assert d.is_celebrating(now=now + 2.0) is True


def test_ref_only_in_packed_refs_resolves(tmp_path):
    now = time.time()
    git = _make_repo(tmp_path, "myrepo")
    (git / "refs" / "heads" / "main").unlink()
    packed = git / "packed-refs"
    packed.write_text(f"# pack-refs with: peeled\n{SHA_A} refs/heads/main\n")
    os.utime(str(packed), (now - 100.0, now - 100.0))
    _touch(git / "HEAD", now - 100.0)
    d = GitDetector(project_dirs=[str(tmp_path)])
    assert d.is_celebrating(now=now - 50.0) is False
    assert d._head_state[git][1:] == ("refs/heads/main", SHA_A)
    # A loose ref now appears with a new tip.
    _set_ref(git, "refs/heads/main", SHA_B, now - 1.0)
    assert d.is_celebrating(now=now) is True


def test_packed_refs_rewrite_celebrates(tmp_path):
    now = time.time()
    git = _make_repo(tmp_path, "myrepo")
    (git / "refs" / "heads" / "main").unlink()
    packed = git / "packed-refs"
    packed.write_text(f"{SHA_A} refs/heads/main\n")
    os.utime(str(packed), (now - 100.0, now - 100.0))
    _touch(git / "HEAD", now - 100.0)
    d = GitDetector(project_dirs=[str(tmp_path)])
    assert d.is_celebrating(now=now - 50.0) is False
    packed.write_text(f"{SHA_B} refs/heads/main\n")
    os.utime(str(packed), (now - 1.0, now - 1.0))
    assert d.is_celebrating(now=now) is True


def test_slash_branch_name_celebrates(tmp_path):
    now = time.time()
    git = _make_repo(tmp_path, "myrepo")
    _set_head(git, "ref: refs/heads/fix/x\n", now - 100.0)
    _set_ref(git, "refs/heads/fix/x", SHA_A, now - 100.0)
    d = GitDetector(project_dirs=[str(tmp_path)])
    assert d.is_celebrating(now=now - 50.0) is False
    _set_ref(git, "refs/heads/fix/x", SHA_B, now - 1.0)
    assert d.is_celebrating(now=now) is True


def test_detached_head_neither_celebrates_nor_crashes(tmp_path):
    now = time.time()
    git, d = _based(tmp_path, now)
    _set_head(git, SHA_B + "\n", now - 10.0)
    assert d.is_celebrating(now=now - 9.0) is False
    _set_head(git, SHA_C + "\n", now - 1.0)  # commit while detached
    assert d.is_celebrating(now=now) is False


def test_unborn_branch_neither_celebrates_nor_crashes(tmp_path):
    now = time.time()
    git = _make_repo(tmp_path, "myrepo")
    (git / "refs" / "heads" / "main").unlink()
    _touch(git / "HEAD", now - 1.0)
    d = GitDetector(project_dirs=[str(tmp_path)])
    assert d.is_celebrating(now=now) is False
    assert d.is_celebrating(now=now + 1.0) is False


def test_hostile_head_ref_is_ignored(tmp_path):
    now = time.time()
    git, d = _based(tmp_path, now)
    _set_head(git, "ref: refs/../../outside\n", now - 1.0)
    assert d.is_celebrating(now=now) is False
    _set_head(git, "ref: /etc/passwd\n", now)
    assert d.is_celebrating(now=now + 1.0) is False


def test_future_ref_mtime_does_not_celebrate(tmp_path):
    now = time.time()
    git, d = _based(tmp_path, now)
    _set_ref(git, "refs/heads/main", SHA_B, now + 3600.0)
    assert d.is_celebrating(now=now) is False


def test_index_touch_with_commit_in_same_tick_is_not_busy(tmp_path):
    now = time.time()
    git, d = _based(tmp_path, now)
    _touch(git / "index", now - 1.0)
    _set_ref(git, "refs/heads/main", SHA_B, now - 1.0)
    assert d.is_busy(now=now) is False
    assert d.is_celebrating(now=now) is True


def test_index_only_fires_busy_not_celebrating(tmp_path):
    git = _make_repo(tmp_path, "myrepo")
    now = time.time()
    _touch(git / "HEAD", now - 100.0)
    _touch(git / "index", now - 1.0)
    _touch(git / "refs" / "heads", now - 100.0)
    d = GitDetector(project_dirs=[str(tmp_path)])
    assert d.is_busy(now=now) is True
    assert d.is_celebrating(now=now) is False


def test_no_activity_is_quiet(tmp_path):
    git = _make_repo(tmp_path, "myrepo")
    now = time.time()
    _touch(git / "HEAD", now - 999.0)
    _touch(git / "index", now - 999.0)
    _touch(git / "refs" / "heads", now - 999.0)
    d = GitDetector(project_dirs=[str(tmp_path)])
    assert d.is_busy(now=now) is False
    assert d.is_celebrating(now=now) is False


def test_celebrate_hold_lasts_20_seconds(tmp_path):
    git = _make_repo(tmp_path, "myrepo")
    now = time.time()
    _set_ref(git, "refs/heads/main", SHA_A, now - 100.0)
    d = GitDetector(project_dirs=[str(tmp_path)])
    assert d.is_celebrating(now=now - 5.0) is False
    _set_ref(git, "refs/heads/main", SHA_B, now - 1.0)
    assert d.is_celebrating(now=now) is True
    # Even after the underlying signal drops out, the sticky hold should
    # keep firing for CELEBRATE_HOLD_SEC.
    assert d.is_celebrating(now=now + 1.0) is True
    assert d.is_celebrating(now=now + 25.0) is False  # past 20s hold (post-e2e-polish Fix 1)


def test_disabled_detector_returns_false(tmp_path):
    git = _make_repo(tmp_path, "myrepo")
    now = time.time()
    _set_ref(git, "refs/heads/main", SHA_B, now - 1.0)
    d = GitDetector(project_dirs=[str(tmp_path)], enabled=False)
    assert d.is_busy(now=now) is False
    assert d.is_celebrating(now=now) is False


def test_no_repos_under_project_dirs_is_quiet(tmp_path):
    (tmp_path / "not-a-repo" / "src").mkdir(parents=True)
    d = GitDetector(project_dirs=[str(tmp_path)])
    assert d.is_busy(now=time.time()) is False
    assert d.is_celebrating(now=time.time()) is False


def test_max_repos_cap_respected(tmp_path):
    # Create 60 repos -> only first 50 should be discovered.
    for i in range(60):
        _make_repo(tmp_path, f"repo_{i:02d}")
    d = GitDetector(project_dirs=[str(tmp_path)])
    d.is_busy(now=time.time())
    assert len(d._discovered) == GitDetector.MAX_REPOS == 50


def test_discovery_cache_reused_within_60s(tmp_path):
    _make_repo(tmp_path, "myrepo")
    calls = {"n": 0}
    real_walk = os.walk
    def counting_walk(*a, **kw):
        calls["n"] += 1
        return real_walk(*a, **kw)
    d = GitDetector(project_dirs=[str(tmp_path)], walk_fn=counting_walk)
    now = time.time()
    d.is_busy(now=now); d.is_busy(now=now + 1.0); d.is_busy(now=now + 30.0)
    walks_within_cache = calls["n"]
    d.is_busy(now=now + 70.0)  # past cache TTL
    assert calls["n"] > walks_within_cache, "cache should expire after 60s"


def test_does_not_descend_into_node_modules(tmp_path):
    # Repo lives inside node_modules -> should be skipped.
    (tmp_path / "node_modules" / "fake-pkg" / ".git").mkdir(parents=True)
    _make_repo(tmp_path, "real-repo")
    d = GitDetector(project_dirs=[str(tmp_path)])
    d.is_busy(now=time.time())
    names = [g.parent.name for g in d._discovered]
    assert "real-repo" in names
    assert "fake-pkg" not in names


def test_diagnostic_keys_present(tmp_path):
    _make_repo(tmp_path, "myrepo")
    d = GitDetector(project_dirs=[str(tmp_path)])
    d.is_busy(now=time.time())
    diag = d.diagnostic()
    for k in ("name", "enabled", "repos_watched", "celebrate_until"):
        assert k in diag
    assert diag["name"] == "git"
    assert diag["repos_watched"] == 1


def test_sha256_ref_celebrates(tmp_path):
    now = time.time()
    git, d = _based(tmp_path, now)
    _set_ref(git, "refs/heads/main", "a" * 64, now - 40.0)
    d.is_celebrating(now=now - 30.0)
    d._celebrate_until = 0.0  # drop the hold from the sha1 -> sha256 change
    _set_ref(git, "refs/heads/main", "b" * 64, now - 20.0)
    assert d.is_celebrating(now=now - 10.0) is True


def test_unreadable_ref_does_not_erase_baseline(tmp_path):
    now = time.time()
    git, d = _based(tmp_path, now)
    _set_ref(git, "refs/heads/main", "not-a-sha", now - 40.0)
    assert d.is_celebrating(now=now - 30.0) is False
    _set_ref(git, "refs/heads/main", SHA_B, now - 20.0)
    assert d.is_celebrating(now=now - 10.0) is True
