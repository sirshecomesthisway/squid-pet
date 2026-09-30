"""Guard against native smoke tests accepting stale or unrelated evidence."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "native_smoke", Path(__file__).resolve().parents[1] / "tools/native_smoke.py"
)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)


@pytest.fixture(autouse=True)
def load_smoke():
    assert SPEC and SPEC.loader
    SPEC.loader.exec_module(smoke)


def test_rejects_a_developer_machine():
    with pytest.raises(RuntimeError, match="GitHub-hosted"):
        smoke.require_hosted_macos("Darwin", {})


def test_rejects_self_hosted_and_non_mac():
    for system, environment in [("Darwin", "self-hosted"), ("Linux", "github-hosted")]:
        with pytest.raises(RuntimeError, match="GitHub-hosted"):
            smoke.require_hosted_macos(system, {
                "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": environment,
            })


@pytest.mark.parametrize("stamp", [94, 101, float("nan"), float("inf"), True, "100"])
def test_rejects_stale_future_or_invalid_state(stamp):
    assert not smoke.fresh_state({"timestamp": stamp, "state": "working"}, 100, 90)


def test_rejects_previous_boot_and_wrong_forced_state():
    assert not smoke.fresh_state({"timestamp": 99}, 100, 99)
    assert not smoke.fresh_state({"timestamp": 100, "state": "idle"}, 100, 99, "working")


def test_accepts_fresh_requested_state():
    assert smoke.fresh_state({"timestamp": 100, "state": "working"}, 101, 99, "working")


def test_window_selection_rejects_menu_bar_and_other_process():
    entries = [
        {"pid": 42, "width": 38, "height": 24, "alpha": 1, "onscreen": True},
        {"pid": 99, "width": 300, "height": 300, "alpha": 1, "onscreen": True},
        {"pid": 42, "width": 300, "height": 300, "alpha": 0, "onscreen": True},
    ]
    assert smoke.sprite_window(entries, 42, 300, 300) is None
    valid = {"pid": 42, "width": 300, "height": 300, "alpha": 1, "onscreen": True}
    assert smoke.sprite_window(entries + [valid], 42, 300, 300) == valid


def test_wait_timeout_keeps_last_evidence():
    with pytest.raises(TimeoutError, match="stale state"):
        smoke.wait_for("startup", lambda: (False, "stale state"), timeout=0)


def test_wait_does_not_retry_an_assertion_failure():
    def failed():
        raise RuntimeError("process died")
    with pytest.raises(RuntimeError, match="process died"):
        smoke.wait_for("health", failed, timeout=10)


def test_launchd_cannot_hide_a_failed_cold_boot():
    with pytest.raises(RuntimeError, match="launch count"):
        smoke.verify_launchd("\tpid = 42\n\truns = 2\n\tlast exit code = 2\n", 42, 1)
    smoke.verify_launchd("\tpid = 42\n\truns = 1\n", 42, 1)
    smoke.verify_launchd("\tpid = 43\n\truns = 2\n", 43, 2)


def test_launchd_missing_count_or_wrong_pid_fails_closed():
    for text in ["\tpid = 42\n", "\truns = 1\n\tpid = 99\n"]:
        with pytest.raises(RuntimeError):
            smoke.verify_launchd(text, 42, 1)


def test_vanished_native_window_fails_even_when_doctor_passes():
    assert not smoke.native_healthy({"sprite": None}, 0, {"healthy": True})
    assert smoke.native_healthy({"sprite": {"id": 7}}, 0, {"healthy": True})


def test_diagnostic_screenshot_timeout_is_recorded(tmp_path, monkeypatch):
    import subprocess
    harness = smoke.Smoke(tmp_path, tmp_path, tmp_path)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args, 10)
    monkeypatch.setattr(harness, "command", timeout)
    harness.screenshot("working", {"id": 7})
    assert harness.events[-1]["captured"] is False
    assert "timed out" in harness.events[-1]["reason"]


def test_startup_rejects_window_lost_during_health_interval(tmp_path, monkeypatch):
    import subprocess
    harness = smoke.Smoke(tmp_path, tmp_path, tmp_path)
    harness.state_dir = tmp_path
    (tmp_path / "pid").write_text("42")
    (tmp_path / "state.json").write_text('{"timestamp": 100}')
    monkeypatch.setattr(smoke.time, "time", lambda: 101)
    monkeypatch.setattr(harness, "assert_alive", lambda pid: None)
    monkeypatch.setattr(harness, "health", lambda pid: None)
    windows = iter([{"sprite": {"id": 7}}, {"sprite": None}])
    monkeypatch.setattr(harness, "windows", lambda pid: next(windows))
    def command(*args, **kwargs):
        if args[0] == "launchctl":
            return subprocess.CompletedProcess(args, 0, "\tpid = 42\n\truns = 1\n")
        return subprocess.CompletedProcess(args, 0, '{"healthy": true}')
    monkeypatch.setattr(harness, "command", command)
    with pytest.raises(RuntimeError, match="native health lost"):
        harness.startup(99)


def test_health_rejects_a_stalled_watcher(tmp_path, monkeypatch):
    harness = smoke.Smoke(tmp_path, tmp_path, tmp_path)
    harness.state_dir = tmp_path
    (tmp_path / "state.json").write_text('{"timestamp": 94}')
    monkeypatch.setattr(smoke.time, "time", lambda: 100)
    monkeypatch.setattr(harness, "assert_alive", lambda pid: None)
    with pytest.raises(RuntimeError, match="stopped ticking"):
        harness.health(42)
