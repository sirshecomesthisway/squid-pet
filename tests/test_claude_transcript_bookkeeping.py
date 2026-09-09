"""Background ledger appends must not impersonate a new Claude turn."""
import json
import os
from datetime import datetime, timezone

import pytest

from squid_pet.detectors import ClaudeCodeDetector


def detector(root):
    return ClaudeCodeDetector(
        projects_dir=root, find_processes_fn=lambda: [object()],
        aggregate_cpu_fn=lambda _: 0,
        has_active_shell_children_fn=lambda _: False,
        shell_cmdline_fn=lambda _: None, recent_file_ages_fn=lambda: [],
    )


def write_transcript(path, activity_time, mtime, ledger_count=1):
    path.parent.mkdir(exist_ok=True)
    record = {'type': 'assistant', 'timestamp': datetime.fromtimestamp(
        activity_time, timezone.utc).isoformat(), 'message': {'content': 'hello'}}
    path.write_text(json.dumps(record) + '\n' +
                    (json.dumps({'type': 'artifact-autoreact-ledger'}) + '\n') * ledger_count)
    os.utime(path, (mtime, mtime))


@pytest.mark.parametrize('ledger_count', [1, 3000])
def test_old_response_with_fresh_background_ledger_is_quiet(tmp_path, ledger_count):
    path = tmp_path / 'project' / 'session.jsonl'
    write_transcript(path, 100, 1000, ledger_count)
    assert detector(tmp_path).is_busy(1001) is False


def test_recent_response_followed_by_ledger_still_counts(tmp_path):
    path = tmp_path / 'project' / 'session.jsonl'
    write_transcript(path, 995, 1000)
    d = detector(tmp_path)
    assert d.is_busy(1001) is True
    assert d.is_busy(1016) is False


def test_real_activity_after_background_append_is_detected(tmp_path):
    path = tmp_path / 'project' / 'session.jsonl'
    write_transcript(path, 100, 1000)
    d = detector(tmp_path)
    assert d.is_busy(1001) is False
    write_transcript(path, 1002, 1002, ledger_count=0)
    assert d.is_busy(1003) is True


def test_ledger_after_unknown_record_preserves_activity_fallback(tmp_path):
    path = tmp_path / 'project' / 'session.jsonl'
    path.parent.mkdir()
    path.write_text('{"type":"future-record"}\n'
                    '{"type":"artifact-autoreact-ledger"}\n')
    os.utime(path, (1000, 1000))
    assert detector(tmp_path).is_busy(1001) is True
