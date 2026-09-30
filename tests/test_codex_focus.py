"""Codex approval clicks must never route through a random Claude process."""
import json
import os

import pytest

from squid_pet import focus, watcher


@pytest.fixture
def waiting(tmp_path, monkeypatch):
    directory = tmp_path / 'codex_awaiting_input'
    directory.mkdir(exist_ok=True)
    marker = directory / ('a' * 64 + '.' + 'b' * 64 + '.' + 'c' * 64)
    marker.touch()
    monkeypatch.setattr(watcher, 'CODEX_AWAITING_INPUT_DIR', str(directory))
    monkeypatch.setattr(watcher, 'claude_sessions_awaiting_input', lambda: [])
    monkeypatch.setattr(focus, '_any_claude_tty', lambda: '/dev/ttysCLAUDE')
    monkeypatch.setattr(watcher, 'find_terminal_app_bundle_for_claude_code',
                        lambda: focus.TERMINAL_APP_BUNDLE_ID)
    monkeypatch.setitem(focus.STATE_SIGNAL_DIRS, 'approval_needed', str(tmp_path / 'claude'))
    return marker


def test_unknown_codex_owner_never_raises_inactive_claude(waiting):
    seen = []
    assert focus.focus_for_state('approval_needed', run=lambda s: seen.append(s) or 'matched') == 'none'
    assert seen == []


def test_cleared_approval_never_falls_back_to_inactive_claude(waiting):
    waiting.unlink()
    seen = []
    assert focus.focus_for_state('approval_needed', run=lambda s: seen.append(s) or 'matched') == 'none'
    assert seen == []


@pytest.mark.parametrize('host, expected', [('Terminal', 'matched'), ('UnsupportedHost', 'none')])
def test_codex_owner_targets_only_supported_exact_terminal_tab(waiting, monkeypatch, host, expected):
    import psutil

    from squid_pet import codex_turns

    class Proc:
        def create_time(self):
            return 50

        def terminal(self):
            return '/dev/ttysCODEX'

        def name(self):
            return host

        def exe(self):
            return '/usr/local/bin/unknown'

        def parent(self):
            return None

    monkeypatch.setattr(codex_turns, 'owner_alive', lambda data: data.get('pid') == 123)
    monkeypatch.setattr(psutil, 'Process', lambda pid: Proc())
    waiting.with_name('.owner.' + waiting.name).write_text(json.dumps({'pid': 123, 'created': 50}))
    seen = []
    assert focus.focus_for_state('approval_needed', run=lambda s: seen.append(s) or 'matched') == expected
    if expected == 'none':
        assert seen == []
        return
    assert '/dev/ttysCODEX' in seen[0]
    assert 'ttysCLAUDE' not in seen[0]
    assert seen[0].index('activate') > seen[0].index('if (tty of tb')
    assert 'return "none"' in seen[0]


def test_dead_or_reused_owner_never_raises_other_session(waiting):
    import psutil
    waiting.with_name('.owner.' + waiting.name).write_text(json.dumps(
        {'pid': os.getpid(), 'created': psutil.Process().create_time() - 1}))
    assert focus.focus_for_state('approval_needed', run=lambda s: pytest.fail('wrong window')) == 'none'


def test_hook_owner_metadata_follows_reference_count_and_cleanup(tmp_path, monkeypatch):
    from tests.test_codex_approval_decisions import hook
    monkeypatch.setattr(hook, 'codex_owner', lambda: {'pid': 123, 'created': 50})
    payload = dict(session_id='session', turn_id='turn', tool_name='Bash',
                   tool_input={'command': 'private command text'})
    for _ in range(2):
        hook.handle({**payload, 'hook_event_name': 'PermissionRequest'}, tmp_path)
    directory = tmp_path / 'codex_awaiting_input'
    owner = next(directory.glob('.owner.*'))
    assert json.loads(owner.read_text()) == {'pid': 123, 'created': 50}
    hook.handle({**payload, 'hook_event_name': 'PostToolUse'}, tmp_path)
    assert owner.exists()
    hook.handle({**payload, 'hook_event_name': 'PostToolUse'}, tmp_path)
    assert not owner.exists()
    hook.handle({**payload, 'hook_event_name': 'PermissionRequest'}, tmp_path)
    hook.handle({**payload, 'hook_event_name': 'Stop'}, tmp_path)
    assert not list(directory.glob('.owner.*'))


def test_newest_unresolvable_request_does_not_jump_to_an_older_owner(waiting):
    older = waiting.with_name('older-request')
    older.touch()
    os.utime(older, (1, 1))
    assert focus.focus_for_state('approval_needed', run=lambda s: pytest.fail('wrong session')) == 'none'


def test_legacy_waiting_entry_point_also_refuses_claude_fallback(waiting):
    assert focus.focus_waiting_session(run=lambda s: pytest.fail('wrong session')) == 'none'
