"""Keep the Codex signal tests independent of the user's live conversations."""
import pytest


@pytest.fixture(autouse=True)
def isolate_codex_wait_markers(tmp_path, monkeypatch):
    from squid_pet import watcher

    monkeypatch.setattr(watcher, 'CODEX_AWAITING_INPUT_DIR',
                        str(tmp_path / 'codex_awaiting_input'))
    monkeypatch.setattr(watcher, '_CODEX_SESSION_FLAG_FIRST_SEEN', {})
