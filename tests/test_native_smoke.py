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
