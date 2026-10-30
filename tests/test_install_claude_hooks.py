"""Exercise setup and the installed command without real agent settings/signals."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from squid_pet import claude_hooks

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "claude_pet_hook.py"
SETUP = ROOT / "scripts" / "install_claude_hooks.py"
SETTINGS_PATHS = claude_hooks.settings_paths


def test_merge_migrate_idempotence_backup_and_remove(tmp_path):
    path = tmp_path / "settings.json"
    original = {"theme": "dark", "permissions": {"allow": ["Read"]}, "hooks": {
        "Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": "echo keep"},
            {"type": "command", "command": "/old/clone/scripts/claude_pet_hook.py"}]}],
        "SessionStart": [{"hooks": [{"type": "prompt", "prompt": "keep"}]}]}}
    raw = json.dumps(original)
    path.write_text(raw)
    path.chmod(0o640)
    assert claude_hooks.configure(path)
    data = json.loads(path.read_text())
    assert data["permissions"] == original["permissions"]
    assert data["theme"] == "dark"
    assert data["hooks"]["Stop"][0]["hooks"] == [{"type": "command", "command": "echo keep"}]
    assert data["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]
    backup = path.with_name("settings.json.squid-backup")
    assert backup.read_text() == raw
    assert path.stat().st_mode & 0o777 == 0o640
    assert backup.stat().st_mode & 0o777 == 0o640
    before = path.stat().st_mtime_ns
    assert not claude_hooks.configure(path)
    assert path.stat().st_mtime_ns == before
    assert backup.read_text() == raw
    assert claude_hooks.inspect_settings(path)["status"] == "registered"
    assert claude_hooks.configure(path, remove=True)
    data = json.loads(path.read_text())
    assert data["hooks"]["Stop"] == [{"matcher": "", "hooks": [
        {"type": "command", "command": "echo keep"}]}]
    assert set(data["hooks"]) == {"Stop", "SessionStart"}
    assert not claude_hooks.configure(path, remove=True)


@pytest.mark.parametrize("raw", ["{broken", "[]", '{"hooks": []}',
                               '{"hooks": {"Stop": {}}}',
                               '{"hooks": {"Stop": [{"hooks": [null]}]}}'])
def test_invalid_settings_are_preserved(tmp_path, raw):
    path = tmp_path / "settings.json"
    path.write_text(raw)
    with pytest.raises(ValueError):
        claude_hooks.configure(path)
    assert path.read_text() == raw
    assert not path.with_name("settings.json.squid-backup").exists()
    assert claude_hooks.inspect_settings(path)["status"] == "invalid"


def test_absent_settings_and_remove_do_not_create_files(tmp_path):
    path = tmp_path / "missing" / "settings.json"
    assert claude_hooks.inspect_settings(path)["status"] == "missing"
    assert not claude_hooks.configure(path, remove=True)
    assert not path.parent.exists()
    assert claude_hooks.configure(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert claude_hooks.configure(path, remove=True)
    assert json.loads(path.read_text()) == {}


def test_symlink_to_dotfiles_is_preserved(tmp_path):
    target = tmp_path / "dotfiles" / "settings.json"
    target.parent.mkdir()
    target.write_text('{"theme": "dark"}')
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    claude_hooks.configure(link)
    assert link.is_symlink()
    assert claude_hooks.inspect_settings(link)["status"] == "registered"
    assert json.loads(target.read_text())["theme"] == "dark"


def test_disabled_setting_is_reported_and_not_overridden(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"disableAllHooks": true}')
    claude_hooks.configure(path)
    assert json.loads(path.read_text())["disableAllHooks"] is True
    assert claude_hooks.inspect_settings(path)["status"] == "disabled"
    assert claude_hooks.main(["--settings-file", str(path), "--check"]) == 1


def test_partial_matcher_and_broken_receiver_are_reported(tmp_path):
    path = tmp_path / "settings.json"
    script = tmp_path / "clone" / "claude_pet_hook.py"
    script.parent.mkdir()
    shutil.copy(SCRIPT, script)
    claude_hooks.configure(path, script=script)
    data = json.loads(path.read_text())
    data["hooks"]["PreToolUse"][0]["matcher"] = "^ExitPlanMode$"
    path.write_text(json.dumps(data))
    report = claude_hooks.inspect_settings(path)
    assert report["status"] == "partial"
    assert report["missing_events"] == ["PreToolUse"]
    claude_hooks.configure(path, script=script)
    script.unlink()
    assert claude_hooks.inspect_settings(path)["status"] == "broken"


def test_read_only_check_does_not_create_settings(tmp_path):
    path = tmp_path / "missing" / "settings.json"
    result = subprocess.run([sys.executable, str(SETUP), "--settings-file", str(path),
                             "--check"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "missing" in result.stdout
    assert "Cowork" in result.stdout
    assert not path.parent.exists()


def test_default_targets_cover_desktop_and_custom_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    assert SETTINGS_PATHS() == [tmp_path / ".claude" / "settings.json",
                                tmp_path / "custom" / "settings.json"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    assert len(SETTINGS_PATHS()) == 1


def test_installed_command_handles_desktop_question_and_resolution(tmp_path):
    """Use a clone path containing spaces, and execute exactly the installed hook.

    The same command must run outside the checkout with no PYTHONPATH and emit
    the existing watcher's session flag and Desktop surface metadata.
    No API call or actual Claude session is required for this lifecycle check.
    """
    script = tmp_path / "Squid checkout" / "scripts" / "claude_pet_hook.py"
    script.parent.mkdir(parents=True)
    shutil.copy(SCRIPT, script)
    path = tmp_path / "settings.json"
    claude_hooks.configure(path, script=script)
    data = json.loads(path.read_text())
    env = dict(os.environ, HOME=str(tmp_path), SQUID_PET_HOME=str(tmp_path / "signals"))
    env.pop("PYTHONPATH", None)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","entrypoint":"local-agent"}\n')

    def fire(event, **fields):
        command = data["hooks"][event][0]["hooks"][0]["command"]
        payload = {"session_id": "desktop-session", "hook_event_name": event,
                   "transcript_path": str(transcript), **fields}
        result = subprocess.run(shlex.split(command), input=json.dumps(payload),
                                capture_output=True, text=True, env=env, cwd=tmp_path,
                                timeout=10)
        assert result.returncode == 0, result.stderr

    fire("PreToolUse", tool_name="AskUserQuestion")
    flag = tmp_path / "signals" / "claude_awaiting_input" / "desktop-session"
    assert flag.read_text().splitlines()[0] == "AskUserQuestion"
    surface = tmp_path / "signals" / "claude_session_surface" / "desktop-session"
    assert surface.read_text().strip() == "local-agent"
    fire("PostToolUse", tool_name="AskUserQuestion")
    assert not flag.exists()
    fire("Notification", notification_type="permission_prompt")
    assert flag.exists()
    fire("SessionEnd")
    assert not flag.exists()


def test_removal_does_not_match_shell_snippets_or_other_hooks(tmp_path):
    path = tmp_path / "settings.json"
    original = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "echo /scripts/claude_pet_hook.py"},
        {"type": "command", "command": "echo 'claude_pet_hook.py'"},
    ]}]}}
    path.write_text(json.dumps(original))
    assert not claude_hooks.configure(path, remove=True)
    assert json.loads(path.read_text()) == original


def test_install_stage_and_launcher_remove_use_the_same_setup(tmp_path):
    """Exercise actual shell entry points against a disposable checkout/home."""
    home = tmp_path / "home"
    project = home / "Projects" / "squid-pet"
    for folder in ("scripts", "src/squid_pet", ".venv/bin"):
        (project / folder).mkdir(parents=True)
    for source in (SETUP, SCRIPT):
        shutil.copy(source, project / "scripts" / source.name)
    shutil.copy(ROOT / "src/squid_pet/claude_hooks.py", project / "src/squid_pet/claude_hooks.py")
    (project / "src/squid_pet/__init__.py").touch()
    (project / ".venv/bin/python").symlink_to(sys.executable)
    env = dict(os.environ, HOME=str(home), SQUID_PROJECT=str(project))
    env.pop("CLAUDE_CONFIG_DIR", None)
    # Source only the declarations; avoid launchd, package installs, and UI.
    library = tmp_path / "install-library.sh"
    library.write_text((ROOT / "install.sh").read_text().rsplit('\nmain "$@"', 1)[0])
    result = subprocess.run(["bash", "-c", 'library="$1"; shift; source "$library"; setup_claude_hooks',
                             "test", str(library)], capture_output=True, text=True,
                            env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    path = home / ".claude/settings.json"
    assert claude_hooks.inspect_settings(path)["status"] == "registered"
    result = subprocess.run(["bash", str(ROOT / "bin/squid"), "setup-claude", "--remove"],
                            capture_output=True, text=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(path.read_text()) == {}

    # The uninstall stage uses the same non-destructive removal, before deleting
    # the project; do not execute the rest of the real uninstaller.
    claude_hooks.configure(path, script=project / "scripts/claude_pet_hook.py")
    library.write_text((ROOT / "uninstall.sh").read_text().split('# ─── main ─')[0])
    result = subprocess.run(["bash", "-c", 'library="$1"; shift; source "$library"; remove_claude_hooks',
                             "test", str(library)], capture_output=True, text=True,
                            env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(path.read_text()) == {}
