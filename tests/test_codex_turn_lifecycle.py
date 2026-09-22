"""An open Codex turn must survive silent reasoning, but not its owner."""
import importlib.util
import json
from pathlib import Path

import pytest

from squid_pet import watcher
from tests.test_watcher_codex_cascade import _codex_machine

SPEC = importlib.util.spec_from_file_location(
    'turn_hook', Path(__file__).resolve().parents[1] / 'scripts/codex_pet_hook.py')
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


def event(root, kind, session='a', turn='one'):
    hook.handle(dict(session_id=session, turn_id=turn, hook_event_name=kind), root)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from squid_pet import codex_turns
    monkeypatch.setattr(codex_turns, 'TURN_DIR', tmp_path / 'codex_turn_active')
    monkeypatch.setattr(hook, 'codex_owner', lambda: {'pid': 123, 'created': 50.0})
    monkeypatch.setattr(codex_turns, 'owner_alive', lambda data: data.get('pid') == 123 and data.get('created') == 50.0)
    return tmp_path, codex_turns


def test_silent_codex_turn_stays_thinking_past_streaming_timeout(world, monkeypatch):
    root, _ = world
    sm = _codex_machine(monkeypatch, transcript_age_sec=300)
    event(root, 'UserPromptSubmit')
    assert sm.compute().state == 'thinking'
    assert sm.compute().state_reason == 'codex turn in flight'
    sm._agent_idle_since = watcher.time.time() - 600
    assert sm.compute().state == 'thinking'


@pytest.mark.parametrize('ending', ['Stop', 'Interrupt', 'SessionEnd'])
def test_end_releases_only_its_turn(world, monkeypatch, ending):
    root, turns = world
    _codex_machine(monkeypatch)
    event(root, 'UserPromptSubmit')
    event(root, 'UserPromptSubmit', session='b')
    event(root, ending)
    assert turns.turn_in_flight(watcher.time.time())
    event(root, ending, session='b')
    assert not turns.turn_in_flight(watcher.time.time())


def test_delayed_stop_cannot_clear_new_turn(world, monkeypatch):
    root, turns = world
    _codex_machine(monkeypatch)
    event(root, 'UserPromptSubmit', turn='new')
    event(root, 'Stop', turn='old')
    assert turns.turn_in_flight(watcher.time.time())


def test_dead_or_reused_owner_cannot_borrow_another_live_codex(world, monkeypatch):
    root, turns = world
    sm = _codex_machine(monkeypatch, transcript_age_sec=300)
    event(root, 'UserPromptSubmit')
    monkeypatch.setattr(turns, 'owner_alive', lambda data: False)
    assert sm.compute().state == 'idle'
    assert list(turns.TURN_DIR.glob('[!.]*')) == []


def test_corrupt_future_and_expired_markers_are_ignored(world, monkeypatch):
    root, turns = world
    _codex_machine(monkeypatch)
    event(root, 'UserPromptSubmit')
    path = next(turns.TURN_DIR.glob('[!.]*'))
    original = json.loads(path.read_text())
    for content in ('{', '[]', json.dumps({**original, 'updated': 2_000_000}),
                    json.dumps({**original, 'updated': 1})):
        path.write_text(content)
        assert not turns.turn_in_flight(watcher.time.time())


def test_post_tool_does_not_resurrect_finished_turn(world, monkeypatch):
    root, turns = world
    _codex_machine(monkeypatch)
    event(root, 'UserPromptSubmit')
    event(root, 'Stop')
    event(root, 'PostToolUse')
    assert not turns.turn_in_flight(watcher.time.time())


def test_owner_identity_checks_real_process_creation_time():
    import os

    import psutil

    from squid_pet.codex_turns import owner_alive
    created = psutil.Process().create_time()
    assert owner_alive({'pid': os.getpid(), 'created': created})
    assert not owner_alive({'pid': os.getpid(), 'created': created - 1})
    assert not owner_alive({'pid': -1, 'created': created})


def test_hook_finds_codex_ancestor_not_hook_shell(monkeypatch):
    class Proc:
        pid = 123

        def __init__(self, command):
            self.command = command

        def exe(self):
            return self.command

        def create_time(self):
            return 50.0

        def parents(self):
            return [Proc('/bin/sh'), Proc('/usr/local/bin/codex')]

    import psutil
    monkeypatch.setattr(psutil, 'Process', lambda: Proc('hook'))
    assert hook.codex_owner() == {'pid': 123, 'created': 50.0}


def test_missing_optional_dependency_keeps_approval_hook_operational(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_psutil(name, *args, **kwargs):
        if name == 'psutil':
            raise ImportError('not installed')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_psutil)
    event(tmp_path, 'UserPromptSubmit')
    hook.handle(dict(session_id='a', turn_id='one', hook_event_name='PermissionRequest',
                     tool_name='Bash', tool_input={'command': 'sleep 60'}), tmp_path)
    assert len(list((tmp_path / 'codex_awaiting_input').glob('[!.]*'))) == 1


def test_inaccessible_ancestry_is_advisory(monkeypatch):
    import psutil

    def denied():
        raise psutil.AccessDenied(123)

    monkeypatch.setattr(psutil, 'Process', denied)
    assert hook.codex_owner() is None


def test_pending_approval_outranks_active_turn(world, monkeypatch):
    root, _ = world
    sm = _codex_machine(monkeypatch, transcript_age_sec=300)
    monkeypatch.setattr(watcher, 'CODEX_AWAITING_INPUT_DIR', str(root / 'codex_awaiting_input'))
    event(root, 'UserPromptSubmit')
    hook.handle(dict(session_id='a', turn_id='one', hook_event_name='PermissionRequest',
                     tool_name='Bash', tool_input={'command': 'sleep 60'}), root)
    assert sm.compute().state == 'approval_needed'
