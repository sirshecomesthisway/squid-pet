"""Keep the agent signal tests independent of the user's live conversations."""
import pytest


@pytest.fixture(autouse=True)
def isolate_codex_wait_markers(tmp_path, monkeypatch):
    from squid_pet import watcher

    monkeypatch.setattr(watcher, 'CODEX_AWAITING_INPUT_DIR',
                        str(tmp_path / 'codex_awaiting_input'))
    monkeypatch.setattr(watcher, '_CODEX_SESSION_FLAG_FIRST_SEEN', {})

    monkeypatch.setattr(watcher, "CODEX_THREAD_HISTORY_DB",
                        tmp_path / "thread_history_1.sqlite")

    # compute() reads claude_failed/ (concerned override) and now prunes
    # claude_session_tty/ (sweep_stale_session_ttys) every tick -- point both
    # at tmp so a bare compute() never reads or deletes the developer's real
    # ~/.squid-pet entries.
    monkeypatch.setattr(watcher, 'CLAUDE_FAILED_DIR',
                        str(tmp_path / 'claude_failed'))
    monkeypatch.setattr(watcher, 'CLAUDE_SESSION_TTY_DIR',
                        str(tmp_path / 'claude_session_tty'))
    # focus.STATE_SIGNAL_DIRS snapshots the watcher dir constants at import,
    # so keep its concerned entry pointing at the SAME (now isolated) object,
    # preserving the production invariant that take-me-there reads the same
    # claude_failed/ the override wrote (test_each_active_state_reads_its_own
    # _signal_dir asserts that identity).
    from squid_pet import focus
    monkeypatch.setitem(focus.STATE_SIGNAL_DIRS, 'concerned',
                        watcher.CLAUDE_FAILED_DIR)


@pytest.fixture(autouse=True)
def isolate_codex_turns(tmp_path, monkeypatch):
    from squid_pet import codex_turns
    monkeypatch.setattr(codex_turns, 'TURN_DIR', tmp_path / 'codex_turn_active')
