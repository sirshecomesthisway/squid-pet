"""Contract test between the Python backend and the frontend sprite layer.

The 8 pet-state names live as string literals on BOTH sides of a language
boundary: Python (watcher.STATES / the cascade) and JS (frontend/index.html's
spriteUrl `VALID` set). Nothing at runtime couples them, so a rename or a new
state on one side would drift silently -- the pet would just fall back to
idle.png with no error. These tests pin the two sides together and verify each
state actually has a sprite PNG on disk, all from the existing pytest run (no
JS toolchain required).
"""
import re
from pathlib import Path

from squid_pet import watcher

FRONTEND_DIR = Path(watcher.__file__).parent / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"
SPRITES_DIR = FRONTEND_DIR / "sprites"

# Sprite names that are frontend-only visuals, not backend states: mood-layer
# frames, sub-state animations, and decorations. Everything else in the JS
# VALID set must be a real backend state.
FRONTEND_ONLY_SPRITES = frozenset({
    "drowsy", "stretch", "look-left", "look-right", "heart", "blink",
    "attention_needed",
})

# Backend state -> sprite basename when it differs from the state name.
STATE_SPRITE_ALIASES = {"approval_needed": "attention_needed"}


def _parse_valid_set() -> set[str]:
    """Extract the members of `const VALID = new Set([...])` from index.html."""
    text = INDEX_HTML.read_text()
    m = re.search(r"const VALID = new Set\(\[(.*?)\]\)", text, re.DOTALL)
    assert m, "could not find `const VALID = new Set([...])` in index.html"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _sprite_for(state: str) -> str:
    """Mirror of index.html spriteUrl(): alias, else the state name, else idle."""
    if state in STATE_SPRITE_ALIASES:
        return STATE_SPRITE_ALIASES[state]
    valid = _parse_valid_set()
    return state if state in valid else "idle"


def test_every_backend_state_is_handled_by_frontend():
    """No backend state may silently fall back to idle.png: each must be either
    explicitly aliased (approval_needed) or a member of the JS VALID set."""
    valid = _parse_valid_set()
    unhandled = {
        s for s in watcher.STATES
        if s not in STATE_SPRITE_ALIASES and s not in valid
    }
    assert not unhandled, (
        f"backend states not handled by frontend spriteUrl VALID set: "
        f"{sorted(unhandled)} -- add them to index.html or alias them"
    )


def test_no_stray_backend_state_in_frontend_valid_set():
    """Every VALID member is either a known frontend-only sprite or a real
    backend state -- catches a JS entry that drifted from a renamed state."""
    valid = _parse_valid_set()
    stray = valid - FRONTEND_ONLY_SPRITES - set(watcher.STATES)
    assert not stray, (
        f"index.html VALID has entries that are neither known frontend-only "
        f"sprites nor backend states: {sorted(stray)}"
    )


def test_every_backend_state_resolves_to_existing_sprite():
    for state in watcher.STATES:
        sprite = _sprite_for(state)
        png = SPRITES_DIR / f"{sprite}.png"
        assert png.exists(), (
            f"state {state!r} -> {png.name} but that sprite file is missing"
        )


def test_every_valid_sprite_file_exists():
    for name in _parse_valid_set():
        png = SPRITES_DIR / f"{name}.png"
        assert png.exists(), f"index.html VALID lists {name!r} but {png.name} is missing"


def test_approval_needed_is_aliased_in_spriteurl():
    """The approval_needed -> attention_needed alias must stay in spriteUrl;
    approval_needed has no sprites/approval_needed.png of its own."""
    text = INDEX_HTML.read_text()
    assert 'state === "approval_needed"' in text
    assert "attention_needed" in text
    assert not (SPRITES_DIR / "approval_needed.png").exists()


def test_agent_active_states_are_a_subset_of_states():
    """Internal Python consistency: the active-state set can't name a state the
    backend never emits."""
    assert watcher.StateMachine._AGENT_ACTIVE_STATES <= watcher.STATES
