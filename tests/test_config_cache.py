"""Tests for config.get/set: default precedence and the mtime-gated cache."""
import json
import os

import pytest

from squid_pet import config


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    """Point config at a tmp file and reset the module-level parse cache so
    each test starts from a clean, isolated state."""
    d = tmp_path / ".squid-pet"
    d.mkdir()
    f = d / "config.json"
    monkeypatch.setattr(config, "CONFIG_DIR", d)
    monkeypatch.setattr(config, "CONFIG_FILE", f)
    monkeypatch.setattr(config, "_CACHE", None)
    monkeypatch.setattr(config, "_CACHE_MTIME", 0.0)
    return f


def _write(path, obj):
    path.write_text(json.dumps(obj))


# ── default precedence ────────────────────────────────────────────────

def test_raw_value_wins_over_default_and_defaults(cfg_env):
    _write(cfg_env, {"celebrate_hold_sec": 42})
    assert config.get("celebrate_hold_sec", 99) == 42


def test_absent_key_returns_defaults_when_no_default_given(cfg_env):
    _write(cfg_env, {})
    # celebrate_hold_sec has a DEFAULTS entry (20); no caller default given.
    assert config.get("celebrate_hold_sec") == config.DEFAULTS["celebrate_hold_sec"]


def test_absent_key_with_no_default_and_no_DEFAULTS_is_none(cfg_env):
    _write(cfg_env, {})
    assert config.get("totally_unknown_key") is None


def test_falsy_caller_default_is_respected(cfg_env):
    """Regression: get(key, <falsy>) must return the falsy default, not fall
    through to DEFAULTS. The old `if default is not None` swallowed 0/""/False."""
    _write(cfg_env, {})
    assert config.get("unknown_int", 0) == 0
    assert config.get("unknown_str", "") == ""
    assert config.get("unknown_bool", False) is False


# ── mtime-gated caching ───────────────────────────────────────────────

def test_reparse_only_when_mtime_changes(cfg_env):
    _write(cfg_env, {"muted": True})
    assert config.get("muted") is True

    st = cfg_env.stat()
    # Change the content but pin mtime unchanged -> the cache must NOT reload.
    _write(cfg_env, {"muted": False})
    os.utime(cfg_env, (st.st_atime, st.st_mtime))
    assert config.get("muted") is True  # stale-by-design: file mtime unchanged

    # Bump mtime -> the next read must reload and see the new value.
    os.utime(cfg_env, (st.st_atime, st.st_mtime + 10))
    assert config.get("muted") is False


def test_set_is_visible_to_get_immediately(cfg_env):
    """set() updates the in-process cache, so a get() in the same tick sees
    the write regardless of filesystem mtime granularity."""
    config.set("muted", True)
    assert config.get("muted") is True
    config.set("muted", False)
    assert config.get("muted") is False


def test_set_merges_rather_than_replaces(cfg_env):
    config.set("muted", True)
    config.set("celebrate_hold_sec", 33)
    assert config.get("muted") is True
    assert config.get("celebrate_hold_sec") == 33
    # And it persisted to disk as a merged object.
    on_disk = json.loads(cfg_env.read_text())
    assert on_disk == {"muted": True, "celebrate_hold_sec": 33}


# ── robustness ────────────────────────────────────────────────────────

def test_missing_file_falls_back_to_defaults(cfg_env):
    # cfg_env file was never written.
    assert not cfg_env.exists()
    assert config.get("celebrate_hold_sec") == config.DEFAULTS["celebrate_hold_sec"]
    assert config.get("muted") is False


def test_file_appearing_later_is_picked_up(cfg_env):
    # First read: no file -> defaults, cache reset.
    assert config.get("muted") is False
    # File appears afterwards.
    _write(cfg_env, {"muted": True})
    assert config.get("muted") is True


def test_corrupt_json_falls_back_to_defaults(cfg_env):
    cfg_env.write_text("{not valid json")
    assert config.get("celebrate_hold_sec") == config.DEFAULTS["celebrate_hold_sec"]


def test_non_dict_json_falls_back_to_defaults(cfg_env):
    cfg_env.write_text("[1, 2, 3]")
    assert config.get("muted") is False


def test_toggle_helpers_round_trip(cfg_env):
    assert config.is_muted() is False
    assert config.toggle_muted() is True
    assert config.is_muted() is True
    assert config.approval_alert_enabled() is True
    assert config.toggle_approval_alert() is False
    assert config.approval_alert_enabled() is False
