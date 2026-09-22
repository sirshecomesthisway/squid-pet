"""Run the actual hook without a Codex process or user configuration."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'codex_pet_hook.py'


def send(tmp_path, event, session='session-a', turn='turn-a', tool='Bash', command='echo one'):
    payload = dict(hook_event_name=event, session_id=session, turn_id=turn,
                   tool_name=tool, tool_input={'command': command})
    result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                            text=True, capture_output=True, timeout=5,
                            env={**os.environ, 'SQUID_PET_HOME': str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout == ('{}\n' if event == 'Stop' else '')  # no decisions
    return list((tmp_path / 'codex_awaiting_input').glob('[!.]*'))


def test_permission_request_signals_and_matching_result_clears(tmp_path):
    assert len(send(tmp_path, 'PermissionRequest')) == 1
    assert send(tmp_path, 'PostToolUse') == []


def test_normal_tool_is_not_waiting(tmp_path):
    assert send(tmp_path, 'PreToolUse') == []


def test_question_signals_until_answered(tmp_path):
    assert len(send(tmp_path, 'PreToolUse', tool='request_user_input')) == 1
    assert send(tmp_path, 'PostToolUse', tool='request_user_input') == []


def test_other_tool_or_turn_cannot_clear_wait(tmp_path):
    send(tmp_path, 'PermissionRequest')
    assert len(send(tmp_path, 'PostToolUse', command='echo two')) == 1
    assert len(send(tmp_path, 'Stop', turn='another-turn')) == 1
    assert len(send(tmp_path, 'SessionEnd', session='another-session')) == 1


@pytest.mark.parametrize('event', ['Stop', 'Interrupt', 'SessionEnd'])
def test_end_clears_wait(tmp_path, event):
    send(tmp_path, 'PermissionRequest')
    assert send(tmp_path, event) == []


def test_identical_concurrent_calls_need_both_results(tmp_path):
    send(tmp_path, 'PermissionRequest')
    send(tmp_path, 'PermissionRequest')
    assert len(send(tmp_path, 'PostToolUse')) == 1
    assert send(tmp_path, 'PostToolUse') == []


def test_permission_description_does_not_break_matching(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('codex_hook', SCRIPT)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    event = dict(session_id='s', turn_id='t', tool_name='Bash',
                 tool_input={'command': 'echo safe', 'description': 'Please approve'})
    module.handle({**event, 'hook_event_name': 'PermissionRequest'}, tmp_path)
    module.handle({**event, 'hook_event_name': 'PostToolUse',
                   'tool_input': {'command': 'echo safe'}}, tmp_path)
    assert list((tmp_path / 'codex_awaiting_input').glob('[!.]*')) == []


def test_accepted_async_question_signals_from_post_tool_hook(tmp_path):
    payload = dict(hook_event_name='PostToolUse', session_id='s', turn_id='t',
                   tool_name='request_user_input_async', tool_use_id='call-question',
                   tool_input={'questions': [{'title': 'Choose?', 'options': ['A', 'B']}]},
                   tool_response='{"accepted":true}')
    result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                            text=True, capture_output=True, timeout=5,
                            env={**os.environ, 'SQUID_PET_HOME': str(tmp_path)})
    assert result.returncode == 0
    assert len(list((tmp_path / 'codex_awaiting_input').glob('[!.]*'))) == 1
    # Codex is allowed to keep working and finish its turn with a question open.
    assert len(send(tmp_path, 'PostToolUse', session='s', turn='t')) == 1
    assert len(send(tmp_path, 'Stop', session='s', turn='t')) == 1
    assert send(tmp_path, 'SessionEnd', session='s', turn='t') == []


@pytest.mark.parametrize('reply_turn', [None, 'new-turn'])
def test_user_reply_clears_only_own_async_questions(tmp_path, reply_turn):
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('codex_hook', SCRIPT)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    for session in ('session-a', 'session-b'):
        for call in ('question-one', 'question-two'):
            module.handle(dict(hook_event_name='PostToolUse', session_id=session,
                               turn_id='old-turn', tool_name='request_user_input_async',
                               tool_use_id=call, tool_response={'accepted': True}), tmp_path)
    before = set(send(tmp_path, 'PermissionRequest'))
    assert len(before) == 5
    remaining = set(send(tmp_path, 'UserPromptSubmit', turn=reply_turn))
    expected_removed = {p for p in before
                        if p.name.startswith(module.digest('session-a') + '.')}
    assert len(expected_removed) == 3
    assert remaining == before - expected_removed


def test_new_main_turn_clears_old_approval_cohort(tmp_path):
    send(tmp_path, 'PermissionRequest', session='s', turn='old-turn')
    send(tmp_path, 'UserPromptSubmit', session='s', turn='new-turn')
    assert not list((tmp_path / 'codex_awaiting_input').glob('[!.]*'))
