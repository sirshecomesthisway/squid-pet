"""Real hook markers + read-only decision DB: no tool completion required."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from squid_pet import watcher

SPEC = importlib.util.spec_from_file_location(
    'hook', Path(__file__).resolve().parents[1] / 'scripts/codex_pet_hook.py')
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from squid_pet import codex_approvals
    db = tmp_path / 'logs.sqlite'
    c = sqlite3.connect(db)
    c.execute('CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, target TEXT, feedback_log_body TEXT)')
    c.commit()
    monkeypatch.setattr(codex_approvals, 'LOG_DB', db)
    monkeypatch.setattr(watcher, 'CODEX_AWAITING_INPUT_DIR', str(tmp_path / 'codex_awaiting_input'))
    monkeypatch.setattr(hook.time, 'time_ns', lambda: 100_000_000_000)
    monkeypatch.setattr(codex_approvals, 'clock_ns', lambda: 110_000_000_000)
    yield tmp_path, c
    c.close()


def request(root, command='sleep 60', session='session-a', turn='turn-a', event='PermissionRequest', tool='Bash'):
    hook.handle(dict(hook_event_name=event, session_id=session, turn_id=turn,
                     tool_name=tool, tool_input={'command': command}), root)


def decision(c, session='session-a', turn='turn-a', item='exec-1', stamp=101, choice='Approved'):
    body = (f'session_loop{{thread_id={session}}}: Submission sub=Submission {{ id: "submission", '
            f'op: ExecApproval {{ id: "{item}", turn_id: Some("{turn}"), decision: {choice} }}, '
            'trace: None, parent_turn_id: None, root_turn_id: None }')
    c.execute('INSERT INTO logs(ts,ts_nanos,target,feedback_log_body) VALUES (?,0,?,?)',
              (stamp, 'codex_core::session::handlers', body))
    c.commit()


def waiting():
    return watcher.codex_requests_awaiting_input()


def test_approved_long_command_stops_waving_before_post_tool(env):
    root, c = env
    request(root)
    assert len(waiting()) == 1
    decision(c)
    assert waiting() == []
    assert len(list((root / 'codex_awaiting_input').glob('[!.]*'))) == 1


def test_another_session_or_turn_cannot_resolve_request(env):
    root, c = env
    request(root)
    decision(c, session='other')
    decision(c, turn='other', item='exec-2')
    assert len(waiting()) == 1


@pytest.mark.parametrize('command', ['sleep 60', 'sleep 90'])
def test_concurrent_requests_wait_for_both_decisions(env, command):
    root, c = env
    request(root)
    request(root, command=command)
    decision(c)
    assert waiting()
    decision(c, item='exec-2')
    assert waiting() == []


def test_completion_of_first_request_does_not_clear_second(env):
    root, c = env
    request(root)
    request(root, command='sleep 90')
    decision(c)
    request(root, event='PostToolUse')
    assert waiting()
    decision(c, item='exec-2')
    assert waiting() == []


def test_new_request_while_approved_command_runs_rearms(env):
    root, c = env
    request(root)
    decision(c)
    assert waiting() == []
    request(root, command='sleep 90')
    assert waiting()


def test_answered_permissions_do_not_clear_input_questions(env):
    root, c = env
    request(root)
    request(root, tool='request_user_input', event='PreToolUse')
    decision(c)
    assert len(waiting()) == 1


@pytest.mark.parametrize('stamp', [99, 111])
def test_old_or_future_decision_cannot_resolve_request(env, stamp):
    root, c = env
    request(root)
    decision(c, stamp=stamp)
    assert waiting()


def test_duplicate_decision_record_is_not_a_second_approval(env):
    root, c = env
    request(root)
    request(root)
    decision(c)
    decision(c)
    assert waiting()


def test_missing_database_preserves_pending_request(env):
    root, c = env
    request(root)
    c.close()
    (root / 'logs.sqlite').unlink()
    assert waiting()


@pytest.mark.parametrize('choice', ['ApprovedForSession', 'Denied', 'Abort'])
def test_any_final_human_decision_ends_the_wait(env, choice):
    root, c = env
    request(root)
    decision(c, choice=choice)
    assert waiting() == []


def test_unrelated_running_shell_cannot_dismiss_pending_permission(env, monkeypatch):
    import os
    import time
    root, _ = env
    request(root)
    for path in (root / 'codex_awaiting_input').glob('[!.]*'):
        os.utime(path, (time.time() - 10, time.time() - 10))
    monkeypatch.setattr(watcher, 'claude_sessions_awaiting_input', lambda: [])
    sm = watcher.StateMachine(detectors=[])
    sm._compute_inner = lambda: watcher.PetState(state='working')
    sm._codex_detector = type('OtherCommand', (), {'shell_active': True})()
    assert sm.compute(notify=False).state == 'approval_needed'


def test_always_allow_prefix_decision_clears_immediately(env):
    root, c = env
    request(root)
    decision(c, choice='ApprovedExecpolicyAmendment { command: ["private-command"] }')
    assert waiting() == []


def test_finished_cohort_cannot_answer_a_later_request(env, monkeypatch):
    root, c = env
    request(root)
    decision(c)
    request(root, event='PostToolUse')
    monkeypatch.setattr(hook.time, 'time_ns', lambda: 105_000_000_000)
    request(root)
    assert waiting()


def test_unknown_decision_format_preserves_request(env):
    root, c = env
    request(root)
    decision(c, choice='ApprovedButStillWaiting')
    assert waiting()


def test_pipeline_exits_approval_before_command_finishes(env, monkeypatch):
    root, c = env
    request(root)
    monkeypatch.setattr(watcher, 'claude_sessions_awaiting_input', lambda: [])
    monkeypatch.setattr(watcher, '_CODEX_SESSION_FLAG_FIRST_SEEN', {})
    sm = watcher.StateMachine(detectors=[])
    sm._compute_inner = lambda: watcher.PetState(state='working')
    assert sm.compute(notify=False).state == 'approval_needed'
    decision(c)
    assert sm.compute(notify=False).state == 'working'


@pytest.mark.parametrize('content', ['{broken', '[]', 'x' * 33000])
def test_malformed_or_oversized_ledger_never_hides_wait(env, content):
    root, c = env
    request(root)
    decision(c)
    ledger = next((root / 'codex_awaiting_input').glob('.permissions.*'))
    ledger.write_text(content)
    assert waiting()


def test_locked_database_retains_wait_and_recovers(env):
    root, c = env
    request(root)
    decision(c)
    c.execute('BEGIN EXCLUSIVE')
    try:
        assert waiting()
    finally:
        c.rollback()
    assert waiting() == []


def test_nanosecond_boundary_cannot_reuse_an_earlier_decision(env, monkeypatch):
    root, c = env
    monkeypatch.setattr(hook.time, 'time_ns', lambda: 100_900_000_000)
    request(root)
    decision(c, stamp=100)
    c.execute('UPDATE logs SET ts_nanos=800000000')
    c.commit()
    assert waiting()
    c.execute('UPDATE logs SET ts_nanos=950000000')
    c.commit()
    assert waiting() == []
