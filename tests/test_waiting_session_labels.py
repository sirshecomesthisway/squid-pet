"""Which session is waving? (Pink-2026-09-01)

With several sessions waiting, the flag directory says HOW MANY but its
filenames are uuids -- useless to a person. The project each belongs to is
already derivable from disk: Claude Code stores a session's transcript at
~/.claude/projects/<encoded-cwd>/<session_id>.jsonl, so the session_id
leads to the directory name, and the directory name is the cwd with its
slashes turned into dashes.

Nothing new is stored and no transcript is opened -- this reads path
names only, the same level of access ClaudeCodeDetector already uses when
it globs those files for mtimes.

Encoding rather than decoding is deliberate: '-Users-p-squid-pet' cannot
be decoded unambiguously (a directory name may itself contain a dash), but
encoding a known cwd the same way and comparing strings is exact.
"""
from __future__ import annotations

from squid_pet import watcher


def test_encodes_a_cwd_the_way_claude_code_does():
    assert watcher.encode_project_dir("/Users/pinksmac") == "-Users-pinksmac"
    assert watcher.encode_project_dir("/Users/p/Projects/squid-pet") == (
        "-Users-p-Projects-squid-pet")


def test_finds_the_project_dir_for_a_session(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    (projects / "-Users-p-Projects-squid-pet").mkdir(parents=True)
    (projects / "-Users-p-Projects-squid-pet" / "sess-1.jsonl").write_text("")
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(projects))

    assert watcher.claude_session_project_dir("sess-1") == "-Users-p-Projects-squid-pet"


def test_unknown_session_has_no_project_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert watcher.claude_session_project_dir("nope") is None


class _FakeProc:
    def __init__(self, cwd, tty="/dev/ttys009"):
        self._cwd, self._tty = cwd, tty
    def cwd(self):
        return self._cwd
    def terminal(self):
        return self._tty


def test_label_comes_from_the_live_process_cwd(tmp_path, monkeypatch):
    """The encoded name alone is ambiguous -- "-Users-p-Projects-squid-pet"
    split on dashes answers "pet". The process knows its real cwd."""
    projects = tmp_path / "projects"
    (projects / "-Users-p-Projects-squid-pet").mkdir(parents=True)
    (projects / "-Users-p-Projects-squid-pet" / "sess-2.jsonl").write_text("")
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(watcher, "find_claude_code_processes",
                        lambda: [_FakeProc("/Users/p/Projects/squid-pet")])

    assert watcher.claude_session_label("sess-2") == "squid-pet"


def test_label_falls_back_approximately_with_no_live_process(tmp_path, monkeypatch):
    """Documented imprecision: with nothing to ask, a dashed project name
    cannot be recovered. Still better than a uuid."""
    projects = tmp_path / "projects"
    (projects / "-Users-p-Projects-squid-pet").mkdir(parents=True)
    (projects / "-Users-p-Projects-squid-pet" / "sess-3.jsonl").write_text("")
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [])

    assert watcher.claude_session_label("sess-3") == "pet"


def test_tty_resolves_through_the_matching_process(tmp_path, monkeypatch):
    """This is what makes "take me to it" correct with several sessions
    running -- the payload has no PID, but the project dir identifies the
    process."""
    projects = tmp_path / "projects"
    (projects / "-Users-p-api").mkdir(parents=True)
    (projects / "-Users-p-api" / "sess-4.jsonl").write_text("")
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [
        _FakeProc("/Users/p/other", "/dev/ttys001"),
        _FakeProc("/Users/p/api", "/dev/ttys002"),
    ])

    assert watcher.claude_session_tty("sess-4") == "/dev/ttys002"


# ── Same-cwd disambiguation (Pink-2026-09-16 wrong-window bug) ──────────
# Two sessions in the SAME directory -- one in Terminal, one in Cursor --
# both match the encoded cwd, so cwd alone returns whichever process is
# listed first. The hook records each session's controlling tty; that tty
# is the authoritative key that tells them apart.

def _record_tty(monkeypatch, tmp_path, session_id, tty):
    d = tmp_path / "session_tty"; d.mkdir(exist_ok=True)
    (d / session_id).write_text(tty)
    monkeypatch.setattr(watcher, "CLAUDE_SESSION_TTY_DIR", str(d))


def test_recorded_tty_picks_the_right_same_cwd_session(tmp_path, monkeypatch):
    """The failed session is the Cursor one (/dev/ttys008); a Terminal
    session shares its cwd and is listed first. The recorded tty must win,
    so tty and host both resolve to Cursor -- not the Terminal bystander."""
    projects = tmp_path / "projects"
    (projects / "-Users-p-Projects-squid-pet").mkdir(parents=True)
    (projects / "-Users-p-Projects-squid-pet" / "sess-cursor.jsonl").write_text("")
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [
        _FakeProc("/Users/p/Projects/squid-pet", "/dev/ttys000"),  # Terminal, first
        _FakeProc("/Users/p/Projects/squid-pet", "/dev/ttys008"),  # Cursor
    ])
    _record_tty(monkeypatch, tmp_path, "sess-cursor", "/dev/ttys008")

    assert watcher.claude_session_proc("sess-cursor").terminal() == "/dev/ttys008"
    assert watcher.claude_session_tty("sess-cursor") == "/dev/ttys008"


def test_without_a_recorded_tty_it_falls_back_to_cwd(tmp_path, monkeypatch):
    """Older/detached session with no recorded tty: the cwd match still
    resolves it (correct whenever sessions live in distinct directories)."""
    projects = tmp_path / "projects"
    (projects / "-Users-p-api").mkdir(parents=True)
    (projects / "-Users-p-api" / "sess-old.jsonl").write_text("")
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [
        _FakeProc("/Users/p/other", "/dev/ttys001"),
        _FakeProc("/Users/p/api", "/dev/ttys002"),
    ])
    monkeypatch.setattr(watcher, "CLAUDE_SESSION_TTY_DIR", str(tmp_path / "empty"))

    assert watcher.claude_session_tty("sess-old") == "/dev/ttys002"


def test_stale_recorded_tty_with_no_live_match_falls_back_to_cwd(tmp_path, monkeypatch):
    """A recorded tty whose process is gone must not strand resolution: fall
    through to the cwd match rather than returning nothing."""
    projects = tmp_path / "projects"
    (projects / "-Users-p-api").mkdir(parents=True)
    (projects / "-Users-p-api" / "sess-z.jsonl").write_text("")
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [
        _FakeProc("/Users/p/api", "/dev/ttys002"),
    ])
    _record_tty(monkeypatch, tmp_path, "sess-z", "/dev/ttys999")  # no live proc

    assert watcher.claude_session_tty("sess-z") == "/dev/ttys002"


def test_label_falls_back_to_none_when_unresolvable(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert watcher.claude_session_label("nope") is None


def test_describes_a_single_waiting_session(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    (projects / "-Users-p-api").mkdir(parents=True)
    (projects / "-Users-p-api" / "s1.jsonl").write_text("")
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(watcher, "find_claude_code_processes", lambda: [])

    assert watcher.describe_waiting_sessions(["s1"]) == "api needs you"


def test_describes_two_waiting_sessions_by_name(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    for enc, sid in (("-Users-p-api", "s1"), ("-Users-p-web", "s2")):
        (projects / enc).mkdir(parents=True)
        (projects / enc / f"{sid}.jsonl").write_text("")
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(projects))

    out = watcher.describe_waiting_sessions(["s1", "s2"])
    assert "api" in out and "web" in out
    assert out.endswith("need you"), f"{out!r} must read as a sentence"


def test_many_waiting_sessions_stay_inside_the_bubble(tmp_path, monkeypatch):
    """The bubble caps at MAX_BUBBLE_CHARS; five project names would blow
    straight past it, so past a couple it counts instead of listing."""
    projects = tmp_path / "projects"
    names = ["alpha-service", "beta-service", "gamma-service", "delta-service"]
    for i, n in enumerate(names):
        (projects / f"-Users-p-{n}").mkdir(parents=True)
        (projects / f"-Users-p-{n}" / f"s{i}.jsonl").write_text("")
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(projects))

    out = watcher.describe_waiting_sessions([f"s{i}" for i in range(4)])
    from squid_pet.observer import MAX_BUBBLE_CHARS
    assert len(out) <= MAX_BUBBLE_CHARS, f"{out!r} would be dropped by _pick"
    assert "4" in out, "it should at least say how many"


def test_unresolvable_sessions_still_report_a_count(tmp_path, monkeypatch):
    """A session whose transcript we cannot find must not vanish from the
    count -- 'someone is waiting' is still true and still useful."""
    monkeypatch.setattr(watcher, "CLAUDE_PROJECTS_DIR", str(tmp_path))
    out = watcher.describe_waiting_sessions(["x", "y"])
    assert out is None or "2" in out


# ── stale tty sweep (Pink-2026-09-24, review #7) ───────────────────────────
# claude_session_tty/ is read only by exact session_id, never listed, so it
# had no read-time prune -- SessionEnd was its only removal path. A crashed
# session leaked its entry, and a later reuse of /dev/ttysNNN could then match
# an unrelated process. sweep_stale_session_ttys evicts entries past the same
# 2h crash-safety window the other flag dirs use.
def test_sweep_evicts_stale_tty_entries_but_keeps_fresh_ones(tmp_path, monkeypatch):
    import os
    import time

    d = tmp_path / "session_tty"; d.mkdir()
    monkeypatch.setattr(watcher, "CLAUDE_SESSION_TTY_DIR", str(d))
    now = time.time()

    fresh = d / "sess-live"; fresh.write_text("/dev/ttys002")
    stale = d / "sess-crashed"; stale.write_text("/dev/ttys009")
    old = now - watcher.CLAUDE_SESSION_TTY_STALE_SEC - 60
    os.utime(stale, (old, old))

    watcher.sweep_stale_session_ttys(now)

    assert fresh.exists(), "a fresh entry (live session) must survive the sweep"
    assert not stale.exists(), "a crashed session's stale entry must be evicted"


def test_sweep_is_a_noop_when_the_dir_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "CLAUDE_SESSION_TTY_DIR", str(tmp_path / "nope"))
    watcher.sweep_stale_session_ttys()  # must not raise


def test_sweep_does_not_unlink_an_entry_re_stamped_after_the_listing(tmp_path, monkeypatch):
    """Pink-2026-09-24 (review #3): the TOCTOU window. A hook may os.replace()
    a fresh entry between the sweep's listing and its unlink; re-stat'ing
    immediately before unlink lets us skip an entry that is fresh again."""
    import os
    import time

    d = tmp_path / "session_tty"; d.mkdir()
    monkeypatch.setattr(watcher, "CLAUDE_SESSION_TTY_DIR", str(d))
    now = time.time()
    entry = d / "sess-race"; entry.write_text("/dev/ttys002")
    old = now - watcher.CLAUDE_SESSION_TTY_STALE_SEC - 60
    os.utime(entry, (old, old))

    real_stat = os.stat
    stat_calls = {"n": 0}

    class _Fresh:
        st_mtime = now  # a hook just re-stamped it

    def _fake_stat(path, *a, **k):
        if str(path) == str(entry):
            stat_calls["n"] += 1
            if stat_calls["n"] == 1:
                return real_stat(path)          # listing sees the stale mtime
            return _Fresh()                     # re-stat before unlink: fresh
        return real_stat(path, *a, **k)

    monkeypatch.setattr(watcher.os, "stat", _fake_stat)

    watcher.sweep_stale_session_ttys(now)

    assert "sess-race" in os.listdir(str(d)), "a just-re-stamped entry was unlinked"
    assert stat_calls["n"] == 2, "the sweep must re-stat immediately before unlinking"
