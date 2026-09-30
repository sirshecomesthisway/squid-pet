"""Smoke tests for the `--why` and `--why-json` CLI flags.

These don't try to assert exact emoji output (which depends on
runtime state); they just verify the command exits 0 and produces
parseable output containing the expected sections.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).parent.parent

# Hermeticity (2026-09-24): the subprocess builds watcher's flag-dir constants
# from expanduser("~") at import time, so it otherwise reads the developer's
# real ~/.squid-pet (a live approval_needed flag there made these `squid why`
# runs non-deterministic and could stall). Point HOME at an empty throwaway dir
# so every ~/.squid-pet and ~/.claude read resolves to nothing -- the smoke
# assertions below only care about structural sections, which are state-blind.
_ISOLATED_HOME = tempfile.mkdtemp(prefix="squid-why-home-")


def _run(*flags) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = _ISOLATED_HOME
    env["SQUID_PET_HOME"] = str(Path(_ISOLATED_HOME) / ".squid-pet")
    return subprocess.run(
        # Exercise the real module entry point, isolating only the OS idle
        # call: Quartz can block indefinitely without WindowServer access.
        [sys.executable, "-c",
         "from squid_pet import watcher; "
         "watcher.macos_idle_seconds = lambda: 0.0; "
         "import runpy; runpy.run_module('squid_pet', run_name='__main__')",
         *flags],
        capture_output=True, text=True, timeout=15,
        cwd=str(PROJECT), env=env,
    )


def test_why_human_output_contains_expected_sections():
    """--why prints state header, DETECTORS section, and VERDICT line."""
    result = _run("--why")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = result.stdout
    assert "squid-pet state:" in out
    assert "DETECTORS:" in out
    assert "VERDICT:" in out
    # Default detectors should be listed
    for name in ("claude_code", "codex", "git", "terminal", "ide"):
        assert name in out, f"missing detector {name} in --why output"


def test_why_json_is_valid_json_with_expected_shape():
    """--why-json output must be parseable and have the documented shape."""
    result = _run("--why-json")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    report = json.loads(result.stdout)
    assert "state" in report
    assert "detectors" in report
    assert "verdict" in report
    # State dict has the schema fields
    for k in ("state", "idle_seconds", "agent_idle_seconds", "timestamp"):
        assert k in report["state"], f"missing state.{k}"
    # Each detector entry has the trigger flags
    assert len(report["detectors"]) >= 1
    for d in report["detectors"]:
        for k in ("name", "enabled", "fired_busy",
                  "fired_celebrating", "fired_grooving"):
            assert k in d, f"detector {d.get('name')} missing {k}"
    # Verdict is a non-empty string
    assert isinstance(report["verdict"], str)
    assert len(report["verdict"]) > 0


def test_why_human_surfaces_approval_alert_toggle():
    """Pink-2026-06-29 silent-kill-switch fix: --why MUST surface whether
    'Your turn' alerts are enabled. We learned the hard way that a False
    flag in config.json silently disables every approval_needed override
    in the cascade -- with no visible cue. --why is the diagnostic of last
    resort and must show this."""
    result = _run("--why")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = result.stdout.lower()
    assert "approval alert" in out, (
        "--why output must mention 'approval alert' status. "
        "Got:\n" + result.stdout
    )
    # Should be marked as on/off so Pink can spot the kill switch instantly
    assert ("on" in out) or ("off" in out)


def test_why_json_includes_approval_alert_fields():
    """--why-json must expose the approval-alert config + live Claude Code
    awaiting-input state so scripts/agents can diagnose 'why didn't Squid
    wave her flag?' without reading config.json directly."""
    result = _run("--why-json")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    report = json.loads(result.stdout)
    assert "approval_alert" in report, (
        "--why-json must include a top-level 'approval_alert' block. "
        "Got keys: " + str(list(report.keys()))
    )
    aa = report["approval_alert"]
    for k in ("enabled", "claude_sessions_awaiting", "claude_sessions_eligible"):
        assert k in aa, f"approval_alert.{k} missing; got {aa}"
    assert isinstance(aa["enabled"], bool)
    assert isinstance(aa["claude_sessions_awaiting"], list)
    assert isinstance(aa["claude_sessions_eligible"], list)


def test_why_help_advertises_both_flags():
    result = _run("--help")
    assert result.returncode == 0
    assert "--why" in result.stdout
    assert "--why-json" in result.stdout


# ── Pink-2026-08-26: --why must be side-effect-free ────────────────────
def test_why_json_stays_valid_json_when_approval_needed_is_live(monkeypatch, capsys):
    """Regression test: a genuinely-active approval_needed used to make
    `squid why` re-fire a REAL macOS notification and print noise into
    stdout on every single invocation (a fresh StateMachine's "fire once"
    latch always starts un-armed) -- silently corrupting --why-json
    output for anything piping it to jq/scripts. Verified at the unit
    level (not subprocess) so it stays hermetic -- a subprocess run
    would need a real flag file under the developer's actual
    ~/.squid-pet."""
    from unittest.mock import patch

    from squid_pet import watcher
    from squid_pet.__main__ import _run_why

    monkeypatch.setattr(watcher, "macos_idle_seconds", lambda: 0.0)

    with patch.object(watcher, "claude_sessions_awaiting_input",
                      return_value=["sess-regression-test"]), \
         patch.object(watcher, "_fire_approval_notification") as mock_notify:
        _run_why(json_output=True)

    out = capsys.readouterr().out
    report = json.loads(out)  # must not raise -- stdout must be pure JSON
    assert report["state"]["state"] == "approval_needed"
    mock_notify.assert_not_called(), \
        "--why must never fire a real OS notification as a side effect"
