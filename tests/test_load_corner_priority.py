"""Startup corner resolution (Option A, Pink-2026-09-23).

squid ALWAYS starts bottom-right -- there is no cross-restart corner memory.
The only thing that can move the launch corner is the optional, hand-edited
`starting_corner` power-user override in settings.json. position.json is NOT
consulted at startup anymore (that "resume last corner" path was removed), and
save_corner() no longer exists -- in-session menu snaps / corner cycling move
her live but persist nothing.

The Cocoa bottom-left origin math (corner_origin / _char_bounds) is verified
correct and pinned separately in test_window_constants_agree.py -- this file is
about the resolution policy only.
"""
import json

from squid_pet import window


# ── pure policy ──────────────────────────────────────────────────────
def test_valid_starting_corner_wins():
    assert window._resolve_starting_corner("top-left") == "top-left"
    assert window._resolve_starting_corner("bottom-left") == "bottom-left"


def test_default_bottom_right_when_unset():
    assert window._resolve_starting_corner(None) == "bottom-right"


def test_default_bottom_right_when_invalid():
    assert window._resolve_starting_corner("garbage") == "bottom-right"
    assert window._resolve_starting_corner("") == "bottom-right"


# ── integration through settings.json ────────────────────────────────
def test_load_corner_honors_starting_corner_override(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"starting_corner": "top-left"}))
    monkeypatch.setattr(window, "SETTINGS_FILE", settings)
    assert window.load_corner() == "top-left"


def test_load_corner_defaults_bottom_right_without_starting_corner(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"stroll_mode": "edges"}))  # no starting_corner
    monkeypatch.setattr(window, "SETTINGS_FILE", settings)
    assert window.load_corner() == "bottom-right"


def test_load_corner_defaults_bottom_right_without_settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(window, "SETTINGS_FILE", tmp_path / "nope.json")
    assert window.load_corner() == "bottom-right"


def test_position_json_is_not_consulted(tmp_path, monkeypatch):
    """The old resume-last-corner path is gone: even a position.json at its
    historical location (~/.squid-pet/position.json) must NOT influence the
    launch corner."""
    monkeypatch.setattr(window.Path, "home", classmethod(lambda cls: tmp_path))
    state = tmp_path / ".squid-pet"
    state.mkdir()
    settings = state / "settings.json"
    settings.write_text(json.dumps({"stroll_mode": "edges"}))  # no starting_corner
    (state / "position.json").write_text(json.dumps({"corner": "top-right"}))
    monkeypatch.setattr(window, "SETTINGS_FILE", settings)
    # position.json says top-right, but with no starting_corner she still
    # defaults to bottom-right -- position.json is never read.
    assert window.load_corner() == "bottom-right"
    # and window no longer even exposes the persistence primitives
    assert not hasattr(window, "save_corner")
    assert not hasattr(window, "POSITION_FILE")


def test_load_corner_defaults_bottom_right_when_settings_is_not_an_object(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(["top-left"]))
    monkeypatch.setattr(window, "SETTINGS_FILE", settings)
    assert window.load_corner() == "bottom-right"


def test_load_settings_returns_empty_dict_for_non_object_json(tmp_path, monkeypatch):
    """Every load_settings() caller (PetApi.__init__'s stroll mode included)
    does .get() on the result, so a non-object file must not crash startup."""
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(window, "SETTINGS_FILE", settings)
    for raw in ("[]", "null", '"top-left"', "3"):
        settings.write_text(raw)
        assert window.load_settings() == {}
