"""Content-blind Codex turn failures; synthetic 0.153.4 SQLite rows only."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from squid_pet import watcher
from squid_pet.detectors import CodexDetector

NOW = 1_800_000_000


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / 'thread_history_1.sqlite'
    monkeypatch.setattr(watcher, 'CODEX_THREAD_HISTORY_DB', path, raising=False)
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE thread_turns (
            thread_id TEXT, turn_id TEXT, status TEXT, error_json TEXT,
            started_at INTEGER, completed_at INTEGER,
            PRIMARY KEY (thread_id, turn_id))''')
    return path


def add(db, category='usageLimitExceeded', *, thread='thread', turn='turn',
        status='failed', age=1, started=None, raw=None):
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?)', (
            thread, turn, status,
            raw if raw is not None else json.dumps({
                'codexErrorInfo': category, 'message': 'PRIVATE MESSAGE',
                'additionalDetails': 'PRIVATE DETAILS'}),
            NOW - age - 2 if started is None else started, NOW - age))


def test_missing_db_does_not_create_one(tmp_path, monkeypatch):
    path = tmp_path / 'missing.sqlite'
    monkeypatch.setattr(watcher, 'CODEX_THREAD_HISTORY_DB', path, raising=False)
    assert watcher.codex_freshest_failure(NOW) is None
    assert not path.exists()


def test_empty_db(db):
    assert watcher.codex_freshest_failure(NOW) is None


@pytest.mark.parametrize(('category', 'expected'), [
    ('usageLimitExceeded', 'usage_limited'),
    ('rateLimitExceeded', 'rate_limit'),
    ('badRequest', 'invalid_request'),
    ('unauthorized', 'authentication_failed'),
    ('serverOverloaded', 'codex_overloaded'),
    ('internalServerError', 'codex_server_error'),
    ('other', 'unknown'),
    ({'httpConnectionFailed': {'httpStatusCode': 400}}, 'codex_connection_error'),
    ({'responseStreamDisconnected': {'httpStatusCode': None}}, 'codex_connection_error'),
    ({'responseStreamConnectionFailed': {}}, 'codex_connection_error'),
    ({'responseTooManyFailedAttempts': {}}, 'codex_connection_error'),
    (None, 'unknown'),
    pytest.param('PRIVATE MESSAGE' * 1000, 'unknown', id='oversized-category'),
    ({'PRIVATE KEY': 'PRIVATE VALUE'}, 'unknown'),
])
def test_category_projection(db, category, expected):
    add(db, category)
    assert watcher.codex_freshest_failure(NOW) == expected


@pytest.mark.parametrize('age', [301, -1])
def test_stale_or_future_failure_excluded(db, age):
    add(db, age=age)
    assert watcher.codex_freshest_failure(NOW) is None


@pytest.mark.parametrize('status', ['inProgress', 'completed', 'interrupted', 'newStatus'])
def test_only_explicit_failed_status_counts(db, status):
    add(db, status=status)
    assert watcher.codex_freshest_failure(NOW) is None


def test_freshest_wins_across_threads(db):
    add(db, 'badRequest', thread='old', age=20)
    add(db, 'usageLimitExceeded', thread='new', age=1)
    assert watcher.codex_freshest_failure(NOW) == 'usage_limited'


@pytest.mark.parametrize('status', ['inProgress', 'completed', 'interrupted'])
def test_retry_clears_previous_failure(db, status):
    add(db, age=20)
    add(db, turn='retry', status=status)
    assert watcher.codex_freshest_failure(NOW) is None


def test_retry_in_other_thread_does_not_clear(db):
    add(db, age=20)
    add(db, thread='unrelated', status='inProgress')
    assert watcher.codex_freshest_failure(NOW) == 'usage_limited'


def test_malformed_json_degrades_safely(db):
    add(db, raw='{broken')
    assert watcher.codex_freshest_failure(NOW) == 'unknown'


def test_malformed_newest_row_does_not_hide_older_fresh_failure(db):
    """Pink-2026-09-24 (review #3): the outer CASE had no ELSE, so a malformed
    NEWEST row projected NULL and -- ORDER BY completed_at DESC LIMIT 1 -- made
    the whole query return None, hiding every other fresh failure. With
    ELSE 'unknown' the failed row surfaces as 'unknown' rather than going
    silent."""
    add(db, 'usageLimitExceeded', thread='older', age=30)   # valid, older
    add(db, thread='newest', age=1, raw='{broken')          # malformed, newest
    assert watcher.codex_freshest_failure(NOW) == 'unknown'


def test_unknown_schema_is_noop(db):
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE thread_turns')
    assert watcher.codex_freshest_failure(NOW) is None


def test_corrupt_db_is_noop(db):
    db.write_bytes(b'not SQLite')
    assert watcher.codex_freshest_failure(NOW) is None


def test_unexpected_reader_exception_is_noop(db, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError('unexpected')
    monkeypatch.setattr(sqlite3, 'connect', broken)
    assert watcher.codex_freshest_failure(NOW) is None


def test_wal_failure_visible_and_database_read_only(db, monkeypatch):
    # Keep the writer open so the fresh row exists only in the WAL.
    with sqlite3.connect(db) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        add(db)
        before = db.read_bytes()
        connect = sqlite3.connect
        def readonly(*args, **kwargs):
            conn = connect(*args, **kwargs)
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                conn.execute('DELETE FROM thread_turns')
            return conn
        monkeypatch.setattr(sqlite3, 'connect', readonly)
        assert watcher.codex_freshest_failure(NOW) == 'usage_limited'
        assert db.read_bytes() == before


@pytest.mark.parametrize(('category', 'severity'), [
    ('usage_limited', 'hard'), ('codex_overloaded', 'transient'),
    ('codex_server_error', 'transient'), ('codex_connection_error', 'transient'),
])
def test_concern_mapping(category, severity):
    reason, actual = watcher.concern_for_error_type(category)
    assert actual == severity
    assert 'Claude' not in reason
    if category == 'usage_limited':
        assert 'limit' in reason.lower()


def machine(monkeypatch, enabled=True):
    for name in ('CLAUDE_FAILED_DIR', 'CLAUDE_AWAITING_INPUT_DIR',
                 'CLAUDE_FINISHED_DIR', 'CLAUDE_RECAPPING_DIR',
                 'CLAUDE_TASK_COMPLETE_DIR', 'CLAUDE_TURN_ACTIVE_DIR'):
        monkeypatch.setattr(watcher, name, '/nonexistent')
    monkeypatch.setattr(watcher.time, 'time', lambda: NOW)
    monkeypatch.setattr(watcher, 'macos_idle_seconds', lambda: 0)
    detector = CodexDetector(
        find_processes_fn=lambda: [], aggregate_cpu_fn=lambda p: 0.0,
        has_active_shell_children_fn=lambda p: False,
        glob_fn=lambda p: iter([]), recent_file_ages_fn=lambda: [])
    detector.enabled = enabled
    return watcher.StateMachine(detectors=[detector])


def test_failure_through_state_machine(db, monkeypatch):
    add(db)
    state = machine(monkeypatch).compute(notify=False)
    assert state.state == 'concerned'
    assert state.concern_severity == 'hard'
    assert 'limit' in state.concern_reason.lower()
    assert 'codex' in state.state_reason


def test_codex_concern_has_no_focus_target(db, monkeypatch):
    """Pink-2026-09-24: a Codex failed-turn row carries no reliable process
    owner, so take-me-there has no session to raise -- focus_target must be
    None, and focus_for_snapshot then returns 'none' (a no-op) rather than
    guessing or inheriting a Claude window."""
    from squid_pet import focus

    add(db)
    state = machine(monkeypatch).compute(notify=False)
    assert state.state == 'concerned'
    assert state.focus_target is None
    # A no-target concern must resolve to a hard no-op: no window raised. The
    # run callback would raise if focus_for_snapshot tried to open anything.
    def _must_not_run(_script):
        raise AssertionError("a Codex-sourced concern must not raise any window")
    assert focus.focus_for_snapshot(state, run=_must_not_run) == 'none'


def test_disabled_detector_skips_read(db, monkeypatch):
    add(db)
    sm = machine(monkeypatch, enabled=False)
    def forbidden(*args):
        pytest.fail('disabled detector read its database')
    monkeypatch.setattr(watcher, 'codex_freshest_failure', forbidden, raising=False)
    assert sm.compute(notify=False).state != 'concerned'


def test_approval_still_wins(db, tmp_path, monkeypatch):
    add(db)
    sm = machine(monkeypatch)
    flags = tmp_path / 'awaiting'
    flags.mkdir()
    (flags / 'session').touch()
    os.utime(flags / 'session', (NOW, NOW))
    monkeypatch.setattr(watcher, 'CLAUDE_AWAITING_INPUT_DIR', str(flags))
    assert sm.compute(notify=False).state == 'approval_needed'


def test_large_history_still_finds_fresh_failure(db):
    # The real 0.153.4 schema has no status/completed_at index. A realistic
    # long-lived history must not exhaust the defensive query budget.
    with sqlite3.connect(db) as conn:
        conn.executemany('INSERT INTO thread_turns VALUES (?, ?, ?, NULL, ?, ?)', (
            (f'history-{i}', 'old', 'completed', NOW - 1000, NOW - 900)
            for i in range(70_000)))
    add(db)
    assert watcher.codex_freshest_failure(NOW) == 'usage_limited'
