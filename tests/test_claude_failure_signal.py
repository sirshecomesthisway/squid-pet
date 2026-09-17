"""Pink-2026-09-16: tests for the StopFailure -> concerned signal.

Claude Code fires the StopFailure hook (NOT Stop) when a turn ends because
of an API error -- a usage/rate limit, server overload, auth/billing
failure, etc. -- carrying a machine-readable error_type. scripts/
claude_pet_hook.py writes ~/.squid-pet/claude_failed/<session_id> holding
just that error_type CATEGORY (content-blind, like every other flag). This
is the ONLY inference-free way to know Squid is BLOCKED by an error rather
than merely quiet -- Pink explicitly rejected inferring "concerned" from a
stalled/silent turn (a false worried face is worse than none).

watcher.claude_freshest_failure() reads that dir; concern_for_error_type()
maps the category to a (reason, severity) the frontend tooltip shows; and
StateMachine._apply_failure_override() layers "concerned" over the cascade,
mirroring the approval_needed override it sits just beneath.

The hook script itself is tested separately, as a real subprocess, in
tests/test_claude_pet_hook_script.py.
"""
from __future__ import annotations

import os
import time

import pytest

from squid_pet import watcher
from squid_pet.watcher import StateMachine


@pytest.fixture
def tmp_failed_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude_failed"
    d.mkdir()
    monkeypatch.setattr(watcher, "CLAUDE_FAILED_DIR", str(d))
    return d


def _write(path, error_type: str, mtime_age_sec: float = 0.5) -> None:
    path.write_text(error_type)
    mtime = time.time() - mtime_age_sec
    os.utime(path, (mtime, mtime))


# ── reader: claude_freshest_failure() ───────────────────────────────────
def test_no_dir_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "CLAUDE_FAILED_DIR", str(tmp_path / "nope"))
    assert watcher.claude_freshest_failure() is None


def test_empty_dir_returns_none(tmp_failed_dir):
    assert watcher.claude_freshest_failure() is None


def test_fresh_failure_returns_its_error_type(tmp_failed_dir):
    _write(tmp_failed_dir / "sess-1", "rate_limit", mtime_age_sec=1.0)
    assert watcher.claude_freshest_failure() == "rate_limit"


def test_stale_failure_is_excluded(tmp_failed_dir):
    """Past the fresh window a failure no longer drives concerned -- it stays
    on disk for the crash-safety sweep, but stops counting as live."""
    _write(tmp_failed_dir / "sess-1", "rate_limit",
           mtime_age_sec=watcher.CLAUDE_FAILED_FRESH_SEC + 10)
    assert watcher.claude_freshest_failure() is None


def test_freshest_failure_wins_across_sessions(tmp_failed_dir):
    _write(tmp_failed_dir / "sess-old", "server_error", mtime_age_sec=30)
    _write(tmp_failed_dir / "sess-new", "billing_error", mtime_age_sec=1)
    assert watcher.claude_freshest_failure() == "billing_error"


# ── mapping: concern_for_error_type() ───────────────────────────────────
@pytest.mark.parametrize("error_type", ["rate_limit", "overloaded", "server_error"])
def test_transient_error_types(error_type):
    reason, severity = watcher.concern_for_error_type(error_type)
    assert severity == "transient"
    assert reason  # non-empty human headline


@pytest.mark.parametrize("error_type", [
    "authentication_failed", "oauth_org_not_allowed", "account_on_hold",
    "billing_error", "invalid_request", "model_not_found",
    "max_output_tokens", "cloud_credential_error", "unknown",
])
def test_hard_error_types(error_type):
    reason, severity = watcher.concern_for_error_type(error_type)
    assert severity == "hard"
    assert reason


def test_rate_limit_reason_mentions_the_limit():
    reason, _ = watcher.concern_for_error_type("rate_limit")
    assert "limit" in reason.lower()


def test_unrecognized_error_type_falls_back_to_hard():
    reason, severity = watcher.concern_for_error_type("some_new_error_2027")
    assert severity == "hard"
    assert reason


# ── override: StateMachine._apply_failure_override via compute() ─────────
def _isolate_other_flag_dirs(monkeypatch):
    """Point every other flag dir StateMachine reads at nothing, so only the
    failed dir under test can influence the outcome."""
    monkeypatch.setattr(watcher, "macos_idle_seconds", lambda: 0.0)
    monkeypatch.setattr(watcher, "CLAUDE_AWAITING_INPUT_DIR", "/nonexistent")
    monkeypatch.setattr(watcher, "CODEX_AWAITING_INPUT_DIR", "/nonexistent")
    monkeypatch.setattr(watcher, "CLAUDE_FINISHED_DIR", "/nonexistent")
    monkeypatch.setattr(watcher, "CLAUDE_RECAPPING_DIR", "/nonexistent")
    monkeypatch.setattr(watcher, "CLAUDE_TASK_COMPLETE_DIR", "/nonexistent")
    monkeypatch.setattr(watcher, "CLAUDE_TURN_ACTIVE_DIR", "/nonexistent")


def test_fresh_failure_overrides_idle_cascade_to_concerned(tmp_failed_dir, monkeypatch):
    _isolate_other_flag_dirs(monkeypatch)
    _write(tmp_failed_dir / "sess-1", "rate_limit", mtime_age_sec=1.0)

    sm = StateMachine(detectors=[])
    st = sm.compute(notify=False)

    assert st.state == "concerned"
    assert st.concern_severity == "transient"
    assert "limit" in st.concern_reason.lower()
    assert "StopFailure" in st.state_reason


def test_no_failure_flag_leaves_cascade_untouched(tmp_failed_dir, monkeypatch):
    _isolate_other_flag_dirs(monkeypatch)
    sm = StateMachine(detectors=[])
    st = sm.compute(notify=False)
    assert st.state != "concerned"


def test_dismiss_concern_suppresses_the_worried_face(tmp_failed_dir, monkeypatch):
    """dblclick's 'I saw the error' calm: a fresh failure that WOULD show
    concerned is suppressed once dismissed, without deleting the flag (so the
    same gesture's take_me_there can still resolve the session)."""
    _isolate_other_flag_dirs(monkeypatch)
    monkeypatch.setattr(watcher, "_concern_dismissed_until", 0.0, raising=False)
    flag = tmp_failed_dir / "sess-1"
    _write(flag, "rate_limit", mtime_age_sec=1.0)
    sm = StateMachine(detectors=[])
    now = time.time()

    st = watcher.PetState()
    sm._apply_failure_override(st, now)
    assert st.state == "concerned"        # fresh failure shows

    watcher.dismiss_concern(now)
    assert flag.exists()                  # NOT deleted -- take_me_there needs it

    st2 = watcher.PetState()
    sm._apply_failure_override(st2, now + 1)
    assert st2.state != "concerned"       # dismissed -> calmed


def test_dismiss_concern_re_expires_after_its_window(tmp_failed_dir, monkeypatch):
    _isolate_other_flag_dirs(monkeypatch)
    monkeypatch.setattr(watcher, "_concern_dismissed_until", 0.0, raising=False)
    now = time.time()
    watcher.dismiss_concern(now)
    # A failure that arrives AFTER the dismiss window still shows concerned.
    later = now + watcher.CONCERN_DISMISS_SEC + 1
    _write(tmp_failed_dir / "sess-2", "rate_limit", mtime_age_sec=1.0)
    os.utime(tmp_failed_dir / "sess-2", (later - 1, later - 1))
    sm = StateMachine(detectors=[])
    st = watcher.PetState()
    sm._apply_failure_override(st, later)
    assert st.state == "concerned"


def test_approval_needed_wins_over_concerned(tmp_failed_dir, tmp_path, monkeypatch):
    """If a session is somehow both blocked-on-you AND carrying a failure
    flag, approval_needed -- the state that REQUIRES Pink to act right now --
    stays the prime override."""
    _isolate_other_flag_dirs(monkeypatch)
    awaiting = tmp_path / "claude_awaiting_input"
    awaiting.mkdir()
    (awaiting / "sess-1").write_text("permission_prompt")
    monkeypatch.setattr(watcher, "CLAUDE_AWAITING_INPUT_DIR", str(awaiting))
    _write(tmp_failed_dir / "sess-1", "rate_limit", mtime_age_sec=1.0)

    sm = StateMachine(detectors=[])
    st = sm.compute(notify=False)

    assert st.state == "approval_needed"
