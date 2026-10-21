"""Phase D -- `squid doctor` reports the Claude Code surfaces it can see.

The desktop app bundles its own Claude Code CLI for the Code tab, which lags
the terminal CLI (2.1.92 vs 2.1.296 on the dev machine), so doctor reports both
versions and warns on a wide skew. Purely informational: a missing desktop app
is normal and never fails doctor or changes its exit code.

Hermetic: the app bundle, the bundled-CLI dir and the terminal CLI's version
are all injected; nothing reads the real /Applications or forks `claude`.
"""
from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from squid_pet import doctor


def _app(root: Path, bundle_id="com.anthropic.claudefordesktop", version="1.1062.0") -> Path:
    app = root / "Claude.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id,
                       "CFBundleShortVersionString": version}, f)
    return app


def _bundled(root: Path, *versions: str) -> Path:
    base = root / "claude-code"
    for v in versions:
        exe = base / v / "claude.app" / "Contents" / "MacOS" / "claude"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
    return base


def _report(tmp_path, *, app=True, bundled=("2.1.92",), terminal="2.1.296", **app_kw):
    apps = [_app(tmp_path, **app_kw)] if app else [tmp_path / "missing" / "Claude.app"]
    root = _bundled(tmp_path, *bundled) if bundled else tmp_path / "no-claude-code"
    return doctor.claude_surface_report(
        app_paths=apps, bundled_cli_root=root,
        terminal_version_fn=lambda: terminal)


def test_reports_both_cli_versions_and_warns_on_a_wide_skew(tmp_path):
    r = _report(tmp_path, bundled=("2.1.80", "2.1.92", "not-a-version"))
    assert r.desktop_app_version == "1.1062.0"
    assert r.desktop_cli_version == "2.1.92", "the newest bundled copy is the one in use"
    assert r.terminal_cli_version == "2.1.296"
    assert len(r.warnings) == 1
    assert "2.1.92" in r.warnings[0] and "2.1.296" in r.warnings[0]
    text = "\n".join(r.lines())
    assert "2.1.92" in text and "2.1.296" in text and "1.1062.0" in text


def test_close_versions_do_not_warn(tmp_path):
    r = _report(tmp_path, bundled=("2.1.290",), terminal="2.1.296")
    assert r.warnings == []


def test_a_minor_version_gap_warns_even_when_patches_are_close(tmp_path):
    r = _report(tmp_path, bundled=("2.0.290",), terminal="2.1.3")
    assert len(r.warnings) == 1


def test_a_missing_desktop_app_is_normal(tmp_path):
    r = _report(tmp_path, app=False, bundled=())
    assert r.desktop_app_version is None and r.desktop_cli_version is None
    assert r.warnings == []
    assert any("not installed" in line for line in r.lines())


def test_an_app_with_another_bundle_id_is_not_the_desktop_app(tmp_path):
    r = _report(tmp_path, bundle_id="com.example.claude-lookalike")
    assert r.desktop_app_version is None


def test_a_missing_terminal_cli_never_warns(tmp_path):
    r = _report(tmp_path, terminal=None)
    assert r.terminal_cli_version is None and r.warnings == []
    assert any("not found" in line for line in r.lines())


def test_desktop_app_present_but_no_bundled_cli_yet(tmp_path):
    """The app downloads its Code-tab CLI on first use."""
    r = _report(tmp_path, bundled=())
    assert r.desktop_app_version == "1.1062.0"
    assert r.desktop_cli_version is None and r.warnings == []


@pytest.mark.parametrize("stdout,expected", [
    ("2.1.296 (Claude Code)\n", "2.1.296"),
    ("claude 10.20.30\n", "10.20.30"),
    ("garbage\n", None),
])
def test_terminal_cli_version_parsing(stdout, expected):
    class _R:
        returncode = 0

    _R.stdout = stdout
    got = doctor.terminal_claude_version(which=lambda name: "/x/claude",
                                         run=lambda *a, **k: _R())
    assert got == expected


def test_terminal_cli_version_never_raises():
    def boom(*a, **k):
        raise OSError("no exec")
    assert doctor.terminal_claude_version(which=lambda n: "/x/claude", run=boom) is None
    assert doctor.terminal_claude_version(which=lambda n: None, run=boom) is None


# ── run_doctor: the section is shown, but never decides the exit code ─────
def _passing_checks(monkeypatch):
    monkeypatch.setattr(doctor, "CHECK_ORDER", (
        ("a", lambda: doctor.CheckResult(name="a", passed=True, diagnostic="ok")),))


def test_run_doctor_prints_surfaces_and_a_skew_warning_does_not_fail(
        tmp_path, monkeypatch, capsys):
    _passing_checks(monkeypatch)
    report = _report(tmp_path)
    monkeypatch.setattr(doctor, "claude_surface_report", lambda: report)
    assert doctor.run_doctor() == 0
    out = capsys.readouterr().out
    assert "1/1 checks passed" in out
    assert "Claude Code surfaces" in out
    assert "2.1.92" in out and "2.1.296" in out


def test_run_doctor_json_carries_the_surface_report(tmp_path, monkeypatch, capsys):
    _passing_checks(monkeypatch)
    report = _report(tmp_path)
    monkeypatch.setattr(doctor, "claude_surface_report", lambda: report)
    assert doctor.run_doctor(json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["healthy"] is True
    surf = payload["claude_surfaces"]
    assert surf["desktop_cli_version"] == "2.1.92"
    assert surf["terminal_cli_version"] == "2.1.296"
    assert len(surf["warnings"]) == 1


def test_run_doctor_survives_a_broken_surface_report(monkeypatch, capsys):
    _passing_checks(monkeypatch)

    def boom():
        raise RuntimeError("plist exploded")

    monkeypatch.setattr(doctor, "claude_surface_report", boom)
    assert doctor.run_doctor() == 0
    assert "1/1 checks passed" in capsys.readouterr().out


def test_bundled_cli_found_under_hash_dir(tmp_path):
    exe = tmp_path / "2.1.304" / "6bbba6adc37f" / "claude.app" / "Contents" / "MacOS" / "claude"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert doctor._desktop_bundled_cli_version(tmp_path) == "2.1.304"
