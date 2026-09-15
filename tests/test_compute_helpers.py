"""Unit tests for the helpers extracted out of StateMachine.compute().

These paths were previously only reachable through the full compute() cascade;
extracting them into named methods lets us pin their behaviour directly.
"""
import pathlib

from squid_pet.watcher import PetState, StateMachine


def _sm():
    # Explicit (empty) detector list -> StateMachine does no settings/disk I/O
    # and owns no detectors; the helpers under test don't need any.
    return StateMachine(detectors=[])


# ── _track_agent_idle ─────────────────────────────────────────────────

def test_idle_clock_starts_and_grows_while_idle():
    sm = _sm()
    now = 1000.0
    st = PetState(state="idle")
    sm._track_agent_idle(st, now)
    assert st.agent_idle_seconds == 0.0   # clock just started this tick
    assert sm._last_state == "idle"

    st2 = PetState(state="idle")
    sm._track_agent_idle(st2, now + 5)
    assert st2.agent_idle_seconds == 5.0  # 5s of continuous idle


def test_idle_clock_resets_to_zero_while_active():
    sm = _sm()
    now = 1000.0
    sm._track_agent_idle(PetState(state="idle"), now)
    st = PetState(state="working")
    sm._track_agent_idle(st, now + 3)
    assert st.agent_idle_seconds == 0.0
    assert sm._agent_idle_since == 0.0
    assert sm._last_state == "working"


def test_idle_clock_restarts_when_leaving_active():
    sm = _sm()
    now = 1000.0
    sm._track_agent_idle(PetState(state="working"), now)   # active
    st = PetState(state="idle")
    sm._track_agent_idle(st, now + 10)                     # just went idle
    # Clock restarts at the moment it left active, so ~0 elapsed this tick.
    assert st.agent_idle_seconds == 0.0


# ── _apply_force_state_override ───────────────────────────────────────

def test_force_state_override_applies(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".squid-pet").mkdir()
    (tmp_path / ".squid-pet" / "force_state").write_text("celebrating\n")
    sm = _sm()
    st = PetState(state="idle")
    sm._apply_force_state_override(st)
    assert st.state == "celebrating"
    assert "force_state override" in st.state_reason


def test_force_state_override_absent_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    sm = _sm()
    st = PetState(state="idle")
    sm._apply_force_state_override(st)
    assert st.state == "idle"


def test_force_state_override_empty_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".squid-pet").mkdir()
    (tmp_path / ".squid-pet" / "force_state").write_text("   \n")
    sm = _sm()
    st = PetState(state="thinking")
    sm._apply_force_state_override(st)
    assert st.state == "thinking"
