import json
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'install_codex_hooks.py'


def install(path, *args):
    result = subprocess.run([sys.executable, str(SCRIPT), '--hooks-file', str(path), *args],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_preserves_other_hooks_and_is_idempotent(tmp_path):
    path = tmp_path / 'hooks.json'
    original = {'description': 'my hooks', 'hooks': {'Stop': [
        {'hooks': [{'type': 'command', 'command': 'echo keep-me'}]}]}}
    path.write_text(json.dumps(original))
    install(path)
    first = json.loads(path.read_text())
    assert first['hooks']['Stop'][0] == original['hooks']['Stop'][0]
    assert 'PermissionRequest' in first['hooks']
    install(path)
    assert json.loads(path.read_text()) == first
    install(path, '--remove')
    assert json.loads(path.read_text()) == original


def test_malformed_file_is_not_overwritten(tmp_path):
    path = tmp_path / 'hooks.json'
    path.write_text('{broken')
    result = subprocess.run([sys.executable, str(SCRIPT), '--hooks-file', str(path)],
                            capture_output=True)
    assert result.returncode != 0
    assert path.read_text() == '{broken'
