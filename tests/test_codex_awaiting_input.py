import time
from unittest.mock import patch

import pytest

from squid_pet import watcher


@pytest.fixture
def flags(tmp_path, monkeypatch):
    directory = tmp_path / 'codex_awaiting_input'
    directory.mkdir()
    monkeypatch.setattr(watcher, 'CODEX_AWAITING_INPUT_DIR', str(directory), raising=False)
    monkeypatch.setattr(watcher, 'claude_sessions_awaiting_input', lambda: [])
    monkeypatch.setattr(watcher, '_CODEX_SESSION_FLAG_FIRST_SEEN', {}, raising=False)
    return directory


def machine(state='working'):
    sm = watcher.StateMachine(detectors=[])
    sm._compute_inner = lambda: watcher.PetState(state=state)
    return sm


def test_codex_wait_overrides_work_and_clears(flags):
    marker = flags / 'request-a'
    marker.touch()
    sm = machine()
    with patch.object(watcher, '_fire_approval_notification') as notify:
        assert sm.compute().state == 'approval_needed'
        assert 'Codex' in sm.compute().state_reason
        notify.assert_called_once_with('your turn', 'Glass', source_label='Codex')
        marker.unlink()
        assert sm.compute().state == 'working'


def test_codex_wait_respects_disabled_alerts(flags):
    (flags / 'request-a').touch()
    with patch('squid_pet.config.get', side_effect=lambda k, default=None:
               False if k == 'approval_alert_enabled' else default):
        assert machine().compute(notify=False).state == 'working'


def test_calm_squid_snoozes_codex_and_new_wait_rearms(flags):
    marker = flags / 'request-a'
    marker.touch()
    assert watcher.count_currently_waving_sessions() == 1
    assert watcher.snooze_all_awaiting_now() >= 1
    sm = machine()
    assert sm.compute(notify=False).state == 'working'
    assert marker.exists()  # acknowledgment does not answer/cancel the request
    assert watcher.codex_requests_awaiting_input() == ['request-a']
    marker.unlink()
    sm.compute(notify=False)
    marker.touch()
    assert sm.compute(notify=False).state == 'approval_needed'


def test_stale_codex_wait_is_pruned(flags):
    import os
    marker = flags / 'request-a'
    marker.touch()
    os.utime(marker, (0, 0))
    assert machine().compute(notify=False).state == 'working'
    assert not marker.exists()


def _codex_detector(shell_active):
    return type('FakeCodex', (), {'name': 'codex', 'shell_active': shell_active})()


def test_codex_wait_clears_once_approved_command_runs(flags):
    # Codex fires no hook when the human grants approval; the only evidence
    # is the approved command actually executing (a live shell child under
    # Codex). When that shows up, stop the wave instead of holding it for the
    # whole command runtime.
    import os
    marker = flags / 'request-a'
    marker.touch()
    os.utime(marker, (time.time() - 5, time.time() - 5))  # past self-heal min age
    sm = machine()
    sm._codex_detector = _codex_detector(shell_active=True)
    assert sm.compute(notify=False).state == 'working'
    assert not marker.exists()


def test_codex_wait_persists_while_awaiting_approval(flags):
    # shell_active is False the whole time the user is still deciding, so a
    # genuinely-pending approval must keep waving and keep its flag.
    import os
    marker = flags / 'request-a'
    marker.touch()
    os.utime(marker, (time.time() - 5, time.time() - 5))
    sm = machine()
    sm._codex_detector = _codex_detector(shell_active=False)
    assert sm.compute(notify=False).state == 'approval_needed'
    assert marker.exists()


def test_fresh_codex_wait_not_reaped_even_if_shell_active(flags):
    # A flag raised this very tick must be seen at least once before the
    # self-heal may reap it -- otherwise a wave could be cleared before it
    # ever shows (the same freshness guard the Claude self-heal uses).
    marker = flags / 'request-a'
    marker.touch()  # age ~0, below SELF_HEAL_MIN_FLAG_AGE_SEC
    sm = machine()
    sm._codex_detector = _codex_detector(shell_active=True)
    assert sm.compute(notify=False).state == 'approval_needed'
    assert marker.exists()
