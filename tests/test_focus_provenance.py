"""Navigation follows the winning state evidence, not another live agent."""
import json
import time
from pathlib import Path

from squid_pet import codex_turns, focus, watcher
from tests.test_watcher_codex_cascade import _codex_machine


def test_codex_shell_state_keeps_the_detected_owner(monkeypatch):
    sm = _codex_machine(monkeypatch, shell_active=True)
    sm._codex_detector.is_busy(watcher.time.time())
    owner = {'pid': 123, 'created': 50}
    sm._codex_detector.shell_owner = owner
    state = sm.compute(notify=False)
    assert state.focus_target == {'agent': 'codex', 'owner': owner}


def test_project_file_activity_does_not_invent_a_workspace(monkeypatch):
    state = _codex_machine(monkeypatch, file_ages=[1]).compute(notify=False)
    assert state.state == 'working'
    assert state.focus_target is None
    assert focus.focus_for_snapshot(state, run=lambda _: (_ for _ in ()).throw(AssertionError())) == 'none'


def test_silent_turn_snapshot_names_the_selected_owner(tmp_path, monkeypatch):
    sm = _codex_machine(monkeypatch, transcript_age_sec=300)
    monkeypatch.setattr(codex_turns, 'owner_alive', lambda data: True)
    codex_turns.TURN_DIR.mkdir()
    for key, pid, age in [('old', 123, 10), ('new', 456, 1)]:
        (codex_turns.TURN_DIR / key).write_text(json.dumps(
            {'pid': pid, 'created': 50, 'updated': time.time() - age}))
    state = sm.compute(notify=False)
    assert state.state == 'thinking'
    assert state.focus_target == {'agent': 'codex', 'owner': {'pid': 456, 'created': 50}}


def test_uncorrelated_codex_transcript_cannot_select_a_random_open_turn(monkeypatch):
    sm = _codex_machine(monkeypatch, transcript_age_sec=1)
    state = sm.compute(notify=False)
    assert state.state == 'thinking'
    assert state.focus_target is None
    assert focus.focus_for_snapshot(state, run=lambda _: 'matched') == 'none'


def test_matching_codex_transcript_hash_selects_its_turn_owner(monkeypatch):
    from squid_pet.codex_approvals import digest

    sm = _codex_machine(monkeypatch, transcript_age_sec=1)
    monkeypatch.setattr(codex_turns, 'owner_alive', lambda data: True)
    codex_turns.TURN_DIR.mkdir()
    (codex_turns.TURN_DIR / 'matching').write_text(json.dumps({
        'pid': 456, 'created': 50, 'updated': time.time(),
        'transcript_key': digest('/fake/.codex/sessions/2026/08/14/s.jsonl'),
    }))
    state = sm.compute(notify=False)
    assert state.state == 'thinking'
    assert state.focus_target == {'agent': 'codex', 'owner': {'pid': 456, 'created': 50}}


def test_hook_transcript_path_hash_matches_detector_turn(monkeypatch, tmp_path):
    import importlib.util

    from squid_pet.codex_approvals import digest

    spec = importlib.util.spec_from_file_location(
        'codex_hook', Path(__file__).resolve().parents[1] / 'scripts/codex_pet_hook.py')
    assert spec and spec.loader
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setattr(hook, 'codex_owner', lambda: {'pid': 456, 'created': 50})
    transcript = '/fixture/codex/session.jsonl'
    hook.handle({
        'hook_event_name': 'UserPromptSubmit', 'session_id': 's', 'turn_id': 't',
        'transcript_path': transcript,
    }, tmp_path)
    monkeypatch.setattr(codex_turns, 'TURN_DIR', tmp_path / 'codex_turn_active')
    monkeypatch.setattr(codex_turns, 'owner_alive', lambda data: True)
    records = codex_turns.active_turns(time.time())
    assert len(records) == 1
    assert records[0]['transcript_key'] == digest(transcript)


def test_unknown_concern_or_generic_celebration_never_opens_claude():
    for name in ('concerned', 'celebrating', 'grooving'):
        state = watcher.PetState(state=name, timestamp=time.time())
        assert focus.focus_for_snapshot(state, run=lambda _: 'matched') == 'none'


def test_claude_snapshot_does_not_switch_to_new_codex_wait(monkeypatch):
    from squid_pet import watcher
    state = watcher.PetState(state='approval_needed', focus_target={'agent': 'claude'})
    monkeypatch.setattr(focus, '_freshest_in', lambda _: 'claude-session')
    monkeypatch.setattr(watcher, 'claude_session_tty', lambda _: '/dev/ttysCLAUDE')
    monkeypatch.setattr(watcher, 'find_terminal_app_bundle_for_claude_code',
                        lambda: focus.TERMINAL_APP_BUNDLE_ID)
    monkeypatch.setattr(watcher, 'codex_requests_awaiting_input',
                        lambda: ['new-codex-request'])
    seen = []
    assert focus.focus_for_snapshot(state, run=lambda script: seen.append(script) or 'app-only') == 'app-only'
    assert seen
    assert 'ttysCLAUDE' in seen[0]


def test_working_hold_preserves_owner_after_shell_quiets(monkeypatch):
    """A working sprite remains tied to the process that started its hold.

    The shell can finish while the agent is still generating; losing the
    exact owner at that boundary made a double-click return ``none``.
    """
    owner = {'pid': 456, 'created': 50}

    class FakeCodex:
        name = 'codex'
        enabled = True
        codex_running = True
        shell_active = True
        shell_owner = owner
        file_active = False
        streaming = False
        transcript_path = None

        def is_busy(self, _now):
            return self.shell_active or self.file_active or self.streaming

        def is_celebrating(self, _now):
            return False

    monkeypatch.setattr(watcher, 'macos_idle_seconds', lambda: 0.0)
    monkeypatch.setattr(watcher, 'CLAUDE_AWAITING_INPUT_DIR', '/nonexistent')
    monkeypatch.setattr(watcher, 'CLAUDE_FINISHED_DIR', '/nonexistent')
    monkeypatch.setattr(watcher, 'CLAUDE_TASK_COMPLETE_DIR', '/nonexistent')
    monkeypatch.setattr(watcher, 'CLAUDE_TURN_ACTIVE_DIR', '/nonexistent')
    monkeypatch.setattr(watcher, 'claude_sessions_awaiting_input', lambda: [])
    monkeypatch.setattr(watcher, 'codex_requests_awaiting_input', lambda: [])
    monkeypatch.setattr(watcher.time, 'time', lambda: 1_000.0)
    monkeypatch.setattr(codex_turns, 'active_turns', lambda _now: [])

    detector = FakeCodex()
    machine = watcher.StateMachine(detectors=[detector])
    first = machine.compute(notify=False)
    assert first.focus_target == {'agent': 'codex', 'owner': owner}

    detector.shell_active = False
    detector.streaming = True
    machine.working_hold_until = 1_025.0
    second = machine.compute(notify=False)
    assert second.state == 'working'
    assert second.focus_target == {'agent': 'codex', 'owner': owner}


def test_unattributed_file_activity_clears_finished_owner(monkeypatch):
    """A later generic write must not keep routing clicks to an old shell."""
    owner = {'pid': 456, 'created': 50}

    class FakeCodex:
        name = 'codex'
        enabled = True
        codex_running = True
        shell_active = True
        shell_owner = owner
        file_active = False
        streaming = False
        transcript_path = None

        def is_busy(self, _now):
            return self.shell_active or self.file_active or self.streaming

        def is_celebrating(self, _now):
            return False

    monkeypatch.setattr(watcher, 'macos_idle_seconds', lambda: 0.0)
    monkeypatch.setattr(watcher, 'CLAUDE_AWAITING_INPUT_DIR', '/nonexistent')
    monkeypatch.setattr(watcher, 'CLAUDE_FINISHED_DIR', '/nonexistent')
    monkeypatch.setattr(watcher, 'CLAUDE_TASK_COMPLETE_DIR', '/nonexistent')
    monkeypatch.setattr(watcher, 'CLAUDE_TURN_ACTIVE_DIR', '/nonexistent')
    monkeypatch.setattr(watcher, 'claude_sessions_awaiting_input', lambda: [])
    monkeypatch.setattr(watcher, 'codex_requests_awaiting_input', lambda: [])
    monkeypatch.setattr(watcher.time, 'time', lambda: 1_000.0)
    monkeypatch.setattr(codex_turns, 'active_turns', lambda _now: [])

    detector = FakeCodex()
    machine = watcher.StateMachine(detectors=[detector])
    assert machine.compute(notify=False).focus_target == {'agent': 'codex', 'owner': owner}

    detector.shell_active = False
    detector.file_active = True
    second = machine.compute(notify=False)
    assert second.state == 'working'
    assert second.focus_target is None
